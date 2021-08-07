from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_sock import Sock


app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = "sqlite:////var/lib/framous/framous.db"
db = SQLAlchemy(app)
sock = Sock(app)


frames = {}


""" MODELS """


slideshow_image = db.Table("slideshow_image",
    db.Column("slideshow_id", db.Integer, db.ForeignKey("slideshow.id"), primary_key=True),
    db.Column("image_id", db.Integer, db.ForeignKey("image.id"), primary_key=True)
)


class Folder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False, unique=True)
    # The parent folder ID. Only the root folder should have a null value.
    folder_id = db.Column(db.Integer, db.ForeignKey("folder.id"))


class Image(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False, unique=True)
    folder_id = db.Column(db.Integer, db.ForeignKey("folder.id"))


class Slideshow(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False, unique=True)
    images = db.relationship(
        "Image",
        secondary=slideshow_image,
        lazy="subquery",
        backref=db.backref("slideshows", lazy=True)
    )


class Frame(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False, unique=True)
    # The slideshow that the frame is currently playing.
    slideshow_id = db.Column(db.Integer, db.ForeignKey("slideshow.id"))


db.create_all()


"""
REST ROUTES

Be explicit about dynamic route types and HTTP methods for better documentation.
"""


# TODO: Paginate
@app.route("/frames", methods=["GET"])
def list_frames():
    return {
        "data": Frame.query.all(),
    }


""" SOCKET ROUTES """


@sock.route("/view")
def view(ws):
    global frames

    frame = Frame(ws)
    frames[frame.uuid] = frame

    while True:
        data = ws.receive()
        ws.send(data)
