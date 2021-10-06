import os
from enum import Enum
from http import HTTPStatus as Status
from flask import Flask, request, Response
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.exc import IntegrityError
from flask_socketio import SocketIO, emit


app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:////var/lib/framous/framous.db"
db = SQLAlchemy(app)
socketio = SocketIO(app)


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
@app.route("/images/<int:id>", methods=["POST"])
def upload_image(id):
    # TODO
    pass


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


@socketio.event
def connect_client(_):
    # Create the client.
    client = Client(sid=request.sid)
    db.session.add(client)
    db.session.commit()

    # If there are frame name request messages, then begin sending them to the
    # client one at a time.
    message = RequestFrameNameMessage.query.first()
    if message:
        message.requested_from_client_sid = request.sid
        db.session.commit()

        emit(
            "request_frame_name",
            {"frame_id": message.frame_id},
            room=request.sid
        )


@socketio.event
def connect_frame(frame_id):
    # New frame
    if not frame_id:
        # Create the frame.
        frame = Frame(sid=request.sid)
        db.session.add(frame)

        # If there are no connected clients, then add a request to be sent when
        # one connects.
        connected_client = Client.query.first()
        if not connected_client:
            message = RequestFrameNameMessage(frame=frame)
            db.session.add(message)

        # Commit the frame (and maybe the message).
        try:
            db.session.commit()
        except IntegrityError as e:
            db.session.rollback()
            # TODO: Check the error message for correctness.
            emit("error", {"message": e.statement}, room=request.sid)
            return

        # Respond with the frame ID so that the device can set it in the
        # browser.
        emit("set_frame_id", {"frame_id": frame.id}, room=request.sid)

        # If there is a connected client, then request it for a name for the
        # new frame.
        if connected_client:
            emit(
                "request_frame_name",
                {"frame_id": frame.id},
                room=connected_client.sid
            )

    # Existing frame
    else:
        frame = db.session.get(Frame, frame_id)
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
def set_frame_name(_):
    pass


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0")
