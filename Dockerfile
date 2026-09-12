FROM python:3.11-alpine
RUN apk add --no-cache tzdata && \
    cp /usr/share/zoneinfo/Asia/Seoul /etc/localtime && \
    echo "Asia/Seoul" > /etc/timezone
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
COPY .env .
RUN mkdir -p data logs
ENV TZ=Asia/Seoul
ENV PYTHONUNBUFFERED=1
CMD ["python", "main.py"]
