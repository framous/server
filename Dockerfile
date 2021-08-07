# Use Buster instead of Alpine due to missing libc dependencies with musl.
FROM python:3-slim-buster
WORKDIR /usr/share/framous
ENV FLASK_APP="src/app.py"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["flask", "run", "--host=0.0.0.0"]
