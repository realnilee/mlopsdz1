# Лёгкий образ имитатора: без torch и весов. Базовый образ закреплён по тегу.
FROM python:3.12.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && pip freeze > /srv/requirements.lock.txt
COPY models/ /models/
COPY app/__init__.py app/server.py app/backends.py app/loadgen.py /srv/app/
EXPOSE 8000
USER 10001
CMD ["python", "-m", "app.server"]
