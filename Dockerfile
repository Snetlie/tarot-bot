FROM python:3.12-slim
WORKDIR /app
COPY . .
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data
RUN mkdir -p /data
CMD ["python", "app.py"]
