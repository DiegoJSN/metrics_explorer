import hashlib
import logging

import redis
from django.conf import settings
from django.db import OperationalError, transaction
from django.utils import timezone

from profiles.models import DailyVisitorsSummary

logger = logging.getLogger(__name__)


class DailyVisitorTrackingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        try:
            self._track_visit(request)
        except OperationalError:
            logger.exception("Failed to track daily visitor data.")
        return response

    def _track_visit(self, request):
        path = request.path or ""
        static_url = getattr(settings, "STATIC_URL", "/static/")
        if path.startswith(static_url) or path.startswith("/healthz"):
            return
        if path not in {"/", "/es/", "/en/"}:
            return

        day = timezone.localdate()
        visitor_id = self._get_visitor_id(request, day)
        unique_created = self._track_unique_visitor(day, visitor_id)

        with transaction.atomic():
            summary, _ = DailyVisitorsSummary.objects.select_for_update().get_or_create(
                day=day,
                defaults={
                    "unique_visitors": 0,
                    "total_visitors": 0,
                },
            )

            summary.total_visitors += 1
            if unique_created:
                summary.unique_visitors += 1

            summary.save(
                update_fields=[
                    "unique_visitors",
                    "total_visitors",
                ]
            )

    def _track_unique_visitor(self, day, visitor_id):
        redis_client = self._get_redis_client()
        if not redis_client:
            return False

        key = f"daily_visitors:{day.isoformat()}:visitors"
        try:
            is_new = redis_client.sadd(key, visitor_id)
            redis_client.expire(key, 60 * 60 * 24)
            return bool(is_new)
        except redis.RedisError:
            logger.exception("Failed to update daily visitor uniqueness in Redis.")
        return False

    def _get_visitor_id(self, request, day):
        return self._build_ephemeral_id(request, day)

    def _build_ephemeral_id(self, request, day):
        ip_address = self._get_client_ip(request)
        user_agent = request.META.get("HTTP_USER_AGENT", "")
        daily_salt = f"{settings.SECRET_KEY}:{day.isoformat()}"
        raw_value = f"{ip_address}|{user_agent}|{daily_salt}"
        return hashlib.sha256(raw_value.encode("utf-8")).hexdigest()

    @staticmethod
    def _get_client_ip(request):
        forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()
        return request.META.get("REMOTE_ADDR", "")

    @staticmethod
    def _get_redis_client():
        redis_url = getattr(settings, "REDIS_URL", "")
        if not redis_url:
            return None
        return redis.Redis.from_url(redis_url)

