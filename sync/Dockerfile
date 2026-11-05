FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY schema.sql views.sql square_sync.py ./
ENTRYPOINT ["python", "square_sync.py"]
