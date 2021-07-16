import uuid


class Frame:

    def __init__(self, ws):
        self.uuid = uuid.uuid4()
        self.ws = ws

    def set_name(self, name):
        self.name = name

    def has_name(self):
        return bool(self.name)
