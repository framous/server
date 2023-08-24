import os
import boto3
from enum import Enum, unique
from http import HTTPStatus as Status
from flask import Flask, request, Response
from werkzeug.utils import secure_filename
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.exc import IntegrityError
from flask_socketio import SocketIO, emit, join_room, close_room


app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:////var/lib/framous/framous.db"
db = SQLAlchemy(app)
socketio = SocketIO(app)

s3 = boto3.resource("s3")


S3_BUCKET = "ders-images"

# TODO: Add support for more of these types.
# https://pillow.readthedocs.io/en/stable/handbook/image-file-formats.html#fully-supported-formats
ALLOWED_EXTENSIONS = ["bmp", "dib", "eps", "gif", "icns", "ico", "im", "jpeg",
                      "jpg", "jpe", "jp2", "jpg2", "jpf", "jpx", "msp", "pcx",
                      "png", "ppm", "sgi", "spider", "tga", "tiff", "webp",
                      "xbm"]


""" MODELS """


slideshow_image = db.Table("slideshow_image",
    db.Column("slideshow_id", db.Integer, db.ForeignKey("slideshow.id"), primary_key=True),
    db.Column("image_id", db.Integer, db.ForeignKey("image.id"), primary_key=True)
)


class Folder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    path = db.Column(db.String(255), nullable=False, unique=True)
    parent_id = db.Column(db.Integer, db.ForeignKey("folder.id"))

    parent = db.relationship("Folder",
        backref="subfolders",
        remote_side=[id]
    )
    images = db.relationship("Image", back_populates="folder")

    def to_dict(self, is_leaf=False):
        data = {
            "id": self.id,
            "path": self.path,
        }

        if not is_leaf:
            data["parent"] = self.parent.to_dict(True) if self.parent else None
            data["subfolders"] = [
                subfolder.to_dict(True) for subfolder in self.subfolders
            ]
            data["images"] = [im.to_dict(True) for im in self.images]

        return data


@unique
class ImageType(Enum):
    ORIGINAL = "original"
    OPTIMIZED = "optimized"
    THUMBNAIL = "thumbnail"


@unique
class StorageType(Enum):
    S3 = range(1)


class Image(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    path = db.Column(db.String(255), nullable=False, unique=True)
    folder_id = db.Column(db.Integer, db.ForeignKey("folder.id"))
    storage_type = db.Column(db.Enum(StorageType))

    folder = db.relationship("Folder", back_populates="images")
    slideshows = db.relationship("Slideshow",
        secondary=slideshow_image,
        back_populates="images"
    )

    def to_dict(self, is_leaf=False):
        # TODO: Move to one-liner if possible.
        if self.storage_type is StorageType.S3:
            url = "s3://"
        else:
            url = "https://"

        data = {
            "id": self.id,
            "path": self.path,
            "storageType": self.storage_type.name,
            "url": url,
        }

        if not is_leaf:
            data["folder"] = self.folder.to_dict(True)
            # TODO: Should slideshows always be included?
            data["slideshows"] = [ss.to_dict() for ss in self.slideshows]

        return data


class Slideshow(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False, unique=True)

    images = db.relationship("Image",
        secondary=slideshow_image,
        back_populates="slideshows"
    )
    frames = db.relationship("Frame", back_populates="slideshow")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "images": [im.to_dict() for im in self.images],
            "frames": [frame.to_dict() for frame in self.frames],
        }


class Frame(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sid = db.Column(db.String(20), unique=True)
    name = db.Column(db.String(50), unique=True)
    # The slideshow that the frame is currently playing.
    slideshow_id = db.Column(db.Integer, db.ForeignKey("slideshow.id"))

    slideshow = db.relationship("Slideshow", back_populates="frames")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "slideshow": self.slideshow.to_dict() if self.slideshow else None,
        }


class Client(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sid = db.Column(db.String(20), unique=True)


class RequestFrameNameMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    frame_id = db.Column(db.Integer, db.ForeignKey("frame.id"), unique=True)
    requested_from_client_sid = db.Column(db.String(20))

    frame = db.relationship("Frame")


db.create_all()

# Create the root folder if it does not exist.
if not Folder.query.filter_by(path="").one_or_none():
    folder = Folder(path="")
    db.session.add(folder)
    db.session.commit()


""" HELPERS """


def allowed_file(filename):
    return "." in filename and \
        filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


""" REST ROUTES """


@app.route("/folders", strict_slashes=False)
@app.route("/folders/<path:path>", methods=["GET", "PUT"])
def folder(path=""):
    if request.method == "GET":
        folder = Folder.query.filter_by(path=path).one_or_none()
        return folder.to_dict() if folder else Response(
            f"Folder {path} not found",
            Status.NOT_FOUND,
            mimetype="text/plain"
        )

    else:
        if Folder.query.filter_by(path=path).one_or_none():
            return Response(
                f"Folder {path} already exists",
                Status.CONFLICT,
                mimetype="text/plain"
            )

        # Get a list of the parent paths, excluding the root path.
        paths = []
        curr_path = path
        while curr_path:
            paths.append(curr_path)
            curr_path, _ = os.path.split(curr_path)

        # Query for a count of the existing parent folders.
        existing_folder_count = Folder.query.filter(
            Folder.path.in_(paths)
        ).count()

        # Compare the count to the number of parent paths to determine which
        # ones need created.
        top_idx_excl = len(paths) - existing_folder_count

        # Create the folders top-down to link the parents to the children.
        # Split to get the first parent in case the parent is the root.
        folders = []
        parent_path, _ = os.path.split(paths[top_idx_excl - 1])
        parent = Folder.query.filter_by(path=parent_path).one()
        for i in reversed(range(top_idx_excl)):
            folder = Folder(path=paths[i], parent=parent)
            folders.append(folder)
            parent = folder

        db.session.add_all(folders)
        db.session.commit()

        return folder.to_dict(), Status.CREATED


# Use POST because the client doesn't know the URI.
@app.route("/images/<string:_>", methods=["POST"])
def create_image(_):
    path = request.json["path"]
    folder_path, _ = os.path.split(path)
    storage_type = request.json["storageType"]

    folder = Folder.query.filter_by(path=folder_path).one_or_none()
    if not folder:
        return Response(
            f"Folder {folder_path} not found",
            Status.NOT_FOUND,
            mimetype="text/plain"
        )

    # Using a try/except pattern here saves a lookup.
    image = Image(
        path=path,
        folder=folder,
        storage_type=StorageType[storage_type]
    )
    db.session.add(image)
    try:
        db.session.commit()
    except (IntegrityError):
        db.session.rollback()
        return Response(
            f"Image {path} already exists",
            Status.CONFLICT,
            mimetype="text/plain"
        )

    # Return the image record for it's ID, which can then be used to upload
    # the corresponding file.
    return image.to_dict(), Status.CREATED


# Use POST to signify that this is not idempotent.
# TODO: Limit the uploaded file size.
@app.route("/images/<int:id>", methods=["POST"])
def upload_image(id):
    image = Image.query.get(id)
    if not image:
        return Response(
            f"Image resource with ID {id} not found",
            Status.NOT_FOUND,
            mimetype="text/plain"
        )

    if "file" not in request.files:
        return Response(
            "No file parts",
            Status.BAD_REQUEST,
            mimetype="text/plain"
        )
    file = request.files["file"]
    if not file.filename:
        return Response(
            "Select a file",
            Status.BAD_REQUEST,
            mimetype="text/plain"
        )
    if not allowed_file(file.filename):
        return Response(
            f"File type '{os.path.splitext(file.filename[1])}' not allowed",
            Status.UNPROCESSABLE_ENTITY,
            mimetype="text/plain"
        )

    # TODO: Add authentication to secure this.
    bucket = s3.Bucket(S3_BUCKET)
    bucket.put_object(
        Key=os.path.join(ImageType.ORIGINAL, image.path),
        Body=file
    )

    # TODO: Implement FS storage.
    # _, fname = os.path.split(image.path)
    # file.save(fname)  # TODO: Use the folder here.

    # TODO: Save thumbnails and compressed versions.
    # fname_no_ext, _ = os.path.splitext(fname)
    # im = Image.open(file)
    # im.thumbnail((99999, 480))
    # im.save(os.path.join(thumbnail_dir, rel_path, f"{fname_no_ext}.webp"), method=6)

    return Response(status=Status.NO_CONTENT)


@app.route("/slideshows")
def slideshows():
    slideshow_pagination = Slideshow.query.paginate()

    return {
        "totalCount": slideshow_pagination.total,
        "data": [ss.to_dict() for ss in slideshow_pagination.items],
    }


@app.route("/slideshows/<string:name>", methods=["GET", "PUT"])
def slideshow(name):
    if request.method == "GET":
        slideshow = Slideshow.query.filter_by(name=name).one_or_none()
        return slideshow.to_dict() if slideshow else Response(
            f"Slideshow {name} not found",
            Status.NOT_FOUND,
            mimetype="text/plain"
        )

    else:
        slideshow = Slideshow(name=name)
        db.session.add(slideshow)
        try:
            db.session.commit()
        except (IntegrityError):
            db.session.rollback()
            return Response(
                f"Slideshow {name} already exists",
                Status.CONFLICT,
                mimetype="text/plain"
            )

        return slideshow.to_dict(), Status.CREATED


# TODO: Add a route for adding all of a folder's images / many images to a
# slideshow. Batch it using `paginate` for "large" adds. Don't forget to handle
# the case where some images are not found.
@app.route("/slideshows/<string:name>/images/<int:id>", methods=["POST"])
def slideshow_images(id):
    slideshow = Slideshow.query.filter_by(name=name).one_or_none()
    if not slideshow:
        return Response(
            f"Slideshow {name} not found",
            Status.NOT_FOUND,
            mimetype="text/plain"
        )

    # Check whether the image is already in the slideshow.
    for im in slideshow.images:
        if im.id == id:
            return Response(
                f"Image with ID {id} is already in slideshow {name}",
                Status.CONFLICT,
                mimetype="text/plain"
            )

    image = Image.query.get(id)
    if not image:
        return Response(
            f"Image with ID {id} not found",
            Status.NOT_FOUND,
            mimetype="text/plain"
        )

    slideshow.images.append(image)
    db.session.add(slideshow)
    db.session.commit()

    return Response(status=Status.NO_CONTENT)


@app.route("/frames")
def frames():
    frame_pagination = Frame.query.paginate()

    return {
        "totalCount": frame_pagination.total,
        "data": [frame.to_dict() for frame in frame_pagination.items],
    }


@app.route("/frames/<string:name>", methods=["GET", "PUT"])
def frame(name):
    if request.method == "GET":
        frame = Frame.query.filter_by(name=name).one_or_none()
        return frame.to_dict() if frame else Response(
            f"Frame {name} not found",
            Status.NOT_FOUND,
            mimetype="text/plain"
        )

    else:
        frame = Frame(name=name)
        db.session.add(frame)
        try:
            db.session.commit()
        except (IntegrityError):
            db.session.rollback()
            return Response(
                f"Frame {name} already exists",
                Status.CONFLICT,
                mimetype="text/plain"
            )

        return frame.to_dict(), Status.CREATED


""" SOCKETIO EVENTS """


# TODO: Add slideshow control flows.
# TODO: If there are multiple frames to name, after completing the naming
# process for one, start on the next one.
# TODO: Add frame and client cleaning flows.


@socketio.event
def connect_client(_):
    # Create the client.
    client = Client(sid=request.sid)
    db.session.add(client)

    # If there are frame name request messages, then begin sending them to the
    # client one at a time.
    message = RequestFrameNameMessage.query.filter(
        RequestFrameNameMessage.requested_from_client_sid == None
    ).first()
    if message:
        message.requested_from_client_sid = request.sid

    try:
        db.session.commit()
        if message:
            emit(
                "request_frame_name",
                {"frame_id": message.frame_id},
                room=request.sid
            )
    except IntegrityError:
        db.session.rollback()
        emit(
            "error",
            {"message": "You are already connected!"},
            room=request.sid
        )


@socketio.event
def connect_frame(json):
    frame_id = json["frame_id"] if "frame_id" in json else None

    # New frame
    if not frame_id:
        # Create the frame.
        frame = Frame(sid=request.sid)
        db.session.add(frame)

        # Add a job for naming the frame.
        message = RequestFrameNameMessage(frame=frame)
        db.session.add(message)

        # If there is a connected client, then request it for a name for the
        # new frame.
        connected_client = Client.query.first()
        if connected_client:
            message.requested_from_client_sid = connected_client.sid
            emit(
                "request_frame_name",
                {"frame_id": frame.id},
                room=connected_client.sid
            )

        # Commit the frame and the job.
        db.session.commit()

        # Respond with the frame ID so that the device can set it in the
        # browser.
        emit("set_frame_id", {"frame_id": frame.id}, room=request.sid)

    # Existing frame
    else:
        frame = db.session.get(Frame, frame_id)

        if frame.sid == request.sid:
            emit(
                "error",
                {"message": "You are already connected!"},
                room=request.sid
            )
            return

        frame.sid = request.sid
        db.session.commit()


@socketio.on("disconnect")
def disconnect():
    # Unassign incomplete frame name requests.
    RequestFrameNameMessage.query.filter_by(
        requested_from_client_sid=request.sid
    ).update({"requested_from_client_sid": None})
    # Delete the client, if it exists (clients are transient).
    Client.query.filter_by(sid=request.sid).delete()

    # Remove the frame's SID, if the frame exists.
    Frame.query.filter_by(sid=request.sid).update({Frame.sid: None})

    db.session.commit()


@socketio.event
def set_frame_name(json):
    frame_id = json["frame_id"]
    name = json["name"]

    # TODO: Normalize and validate the name!

    if Frame.query.filter_by(name=name).one_or_none():
        emit(
            "error",
            {"message": f"The name {name} is already taken"},
            room=request.sid
        )
    else:
        room = "tmp"
        join_room(room, request.sid)
        join_room(room, db.session.get(Frame, frame_id).sid)

        emit("confirm_frame_name", {
            "frame_id": frame_id,
            "name": name
        }, room=room)

        close_room(room)


@socketio.event
def confirm_frame_name(json):
    frame_id = json["frame_id"]
    name = json["name"]
    is_confirmed = json["is_confirmed"]

    frame = db.session.get(Frame, frame_id)

    if not is_confirmed:
        emit("clear_frame_name_confirmation", room=frame.sid)
        return

    frame.name = name
    # The naming job is complete - delete it.
    RequestFrameNameMessage.query.filter_by(frame_id=frame_id).delete()

    # At this point, it would be almost deliberate if there was a naming
    # conflict. Check for it anyway.
    try:
        db.session.commit()
        emit("confirm_frame_name_confirmation", {}, room=frame.sid)
    except IntegrityError:
        db.session.rollback()
        emit("clear_frame_name_confirmation", room=frame.sid)
        emit(
            "error",
            {"message": f"The name {name} is already taken"},
            room=request.sid
        )


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0")
