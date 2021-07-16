from flask import Flask
from flask_sock import Sock
import json
from frame import Frame


app = Flask(__name__)
sock = Sock(app)


frames = {}


@sock.route("/view")
def view(ws):
    global frames

    frame = Frame(ws)
    frames[frame.uuid] = frame

    while True:
        data = ws.receive()
        ws.send(data)
