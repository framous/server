# Use Debian instead of Alpine due to missing libc dependencies with musl.
# Pin the Python version down from 3 to 3.9 due to an eventlet bug in 3.10.
# See https://github.com/eventlet/eventlet/issues/687
FROM python:3.9-slim
RUN useradd --create-home framous
WORKDIR /usr/share/framous

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "src/app.py"]
