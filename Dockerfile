FROM python:3.12-slim

# HOME=/tmp: kontener może działać jako dowolny użytkownik (na Unraid 99:100), gunicorn potrzebuje zapisywalnego katalogu domowego
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
WORKDIR /app

RUN useradd --create-home --uid 1000 app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app . .
RUN DJANGO_DEBUG=1 python manage.py collectstatic --noinput \
    && mkdir -p /data/private && chown -R app:app /data

USER app
ENV PRIVATE_MEDIA_ROOT=/data/private
EXPOSE 8000
CMD ["sh", "-c", "python manage.py migrate --noinput && python manage.py createcachetable && gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 3 --timeout 60 --graceful-timeout 30 --access-logfile -"]
