FROM python:3.12-slim
RUN pip install --no-cache-dir pywebpush tzdata
WORKDIR /srv
COPY app/ /srv/
ENV DATA_DIR=/data
VOLUME /data
EXPOSE 8080
CMD ["python", "-u", "server.py"]
