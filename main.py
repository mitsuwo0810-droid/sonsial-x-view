import asyncio
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from twikit import Client


# ローカル開発では .env を読み込み、Render では環境変数をそのまま使います。
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)


@dataclass(frozen=True)
class Settings:
    """初心者でも編集しやすいよう、設定値をここに集約しています。"""

    app_name: str = os.getenv("APP_NAME", "sonsial x view")
    twikit_username: str = os.getenv("TWIKIT_USERNAME", "")
    twikit_email: str = os.getenv("TWIKIT_EMAIL", "")
    twikit_password: str = os.getenv("TWIKIT_PASSWORD", "")
    twikit_totp_secret: str = os.getenv("TWIKIT_TOTP_SECRET", "")
    twikit_language: str = os.getenv("TWIKIT_LANGUAGE", "ja-JP")
    cookie_file: Path = Path(os.getenv("TWIKIT_COOKIE_FILE", DATA_DIR / "cookies.json"))
    cache_ttl_seconds: int = int(os.getenv("CACHE_TTL_SECONDS", "120"))
    request_min_interval_seconds: float = float(os.getenv("REQUEST_MIN_INTERVAL_SECONDS", "2.0"))
    retry_attempts: int = int(os.getenv("RETRY_ATTEMPTS", "3"))
    retry_backoff_seconds: float = float(os.getenv("RETRY_BACKOFF_SECONDS", "1.5"))
    tweet_count: int = min(int(os.getenv("TWEET_COUNT", "20")), 20)
    default_timeline: str = os.getenv("DEFAULT_TIMELINE", "latest")

    @property
    def can_login(self) -> bool:
        return bool(self.twikit_username and self.twikit_password)


class TTLCache:
    """Twikitへのアクセス回数を減らすための軽量インメモリキャッシュです。"""

    def __init__(self, ttl_seconds: int) -> None:
        self.ttl_seconds = ttl_seconds
        self._items: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        item = self._items.get(key)
        if not item:
            return None

        expires_at, value = item
        if expires_at < time.time():
            self._items.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        self._items[key] = (time.time() + self.ttl_seconds, value)


class TwikitService:
    """ログイン、Cookie保存、リトライ、rate limit対策をまとめたサービス層です。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = Client(settings.twikit_language)
        self.cache = TTLCache(settings.cache_ttl_seconds)
        self._login_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._last_request_at = 0.0
        self._logged_in = False

    async def ensure_login(self, force: bool = False) -> None:
        if self._logged_in and not force:
            return

        async with self._login_lock:
            if self._logged_in and not force:
                return

            if not self.settings.can_login:
                raise RuntimeError(
                    "TWIKIT_USERNAME と TWIKIT_PASSWORD を .env または Render 環境変数に設定してください。"
                )

            self.settings.cookie_file.parent.mkdir(parents=True, exist_ok=True)

            # 次回起動時は保存済みCookieから自動ログインを試します。
            if self.settings.cookie_file.exists() and not force:
                try:
                    self.client.load_cookies(str(self.settings.cookie_file))
                    await self.client.user_id()
                    self._logged_in = True
                    return
                except Exception:
                    self._logged_in = False
                    self.settings.cookie_file.unlink(missing_ok=True)

            if force:
                self.settings.cookie_file.unlink(missing_ok=True)

            # Cookieが無い/期限切れ/検証失敗の場合は再ログインし、成功後にCookieを保存します。
            await self.client.login(
                auth_info_1=self.settings.twikit_username,
                auth_info_2=self.settings.twikit_email or None,
                password=self.settings.twikit_password,
                totp_secret=self.settings.twikit_totp_secret or None,
                cookies_file=str(self.settings.cookie_file),
            )
            self.client.save_cookies(str(self.settings.cookie_file))
            self._logged_in = True

    async def _wait_for_rate_limit(self) -> None:
        """短時間に連続リクエストしないよう、全Twikit呼び出しをゆるく直列化します。"""

        async with self._request_lock:
            elapsed = time.monotonic() - self._last_request_at
            wait_seconds = self.settings.request_min_interval_seconds - elapsed
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)
            self._last_request_at = time.monotonic()

    async def _with_retry(self, action: Callable[[], Awaitable[Any]]) -> Any:
        last_error: Exception | None = None

        for attempt in range(1, self.settings.retry_attempts + 1):
            try:
                await self.ensure_login(force=False)
                await self._wait_for_rate_limit()
                return await action()
            except Exception as error:
                last_error = error
                message = str(error).lower()

                # Cookie期限切れ・CSRF系の失敗は再ログインしてから再試行します。
                if any(word in message for word in ["unauthorized", "forbidden", "csrf", "login"]):
                    await self.ensure_login(force=True)

                if attempt < self.settings.retry_attempts:
                    await asyncio.sleep(self.settings.retry_backoff_seconds * attempt)

        raise last_error or RuntimeError("Twikit request failed")

    async def timeline(self, mode: str) -> list[dict[str, Any]]:
        cache_key = f"timeline:{mode}:{self.settings.tweet_count}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        async def fetch() -> Any:
            if mode == "for_you":
                return await self.client.get_timeline(count=self.settings.tweet_count)
            return await self.client.get_latest_timeline(count=self.settings.tweet_count)

        tweets = [normalize_tweet(tweet) for tweet in await self._with_retry(fetch)]
        self.cache.set(cache_key, tweets)
        return tweets

    async def search(self, query: str, product: str) -> list[dict[str, Any]]:
        product = product if product in {"Top", "Latest", "Media"} else "Latest"
        cache_key = f"search:{product}:{query}:{self.settings.tweet_count}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        async def fetch() -> Any:
            return await self.client.search_tweet(query, product, count=self.settings.tweet_count)

        tweets = [normalize_tweet(tweet) for tweet in await self._with_retry(fetch)]
        self.cache.set(cache_key, tweets)
        return tweets

    async def user_profile(self, screen_name: str) -> dict[str, Any]:
        clean_name = screen_name.lstrip("@")
        cache_key = f"user:{clean_name}:{self.settings.tweet_count}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        async def fetch() -> Any:
            user = await self.client.get_user_by_screen_name(clean_name)
            await self._wait_for_rate_limit()
            tweets = await self.client.get_user_tweets(user.id, "Tweets", count=self.settings.tweet_count)
            return user, tweets

        user, tweets = await self._with_retry(fetch)
        result = {
            "user": normalize_user(user),
            "tweets": [normalize_tweet(tweet) for tweet in tweets],
        }
        self.cache.set(cache_key, result)
        return result


def safe_attr(obj: Any, name: str, default: Any = None) -> Any:
    return getattr(obj, name, default)


def normalize_user(user: Any) -> dict[str, Any]:
    return {
        "id": safe_attr(user, "id", ""),
        "name": safe_attr(user, "name", "Unknown"),
        "screen_name": safe_attr(user, "screen_name", ""),
        "avatar": safe_attr(user, "profile_image_url", ""),
        "banner": safe_attr(user, "profile_banner_url", ""),
        "description": safe_attr(user, "description", ""),
        "followers_count": safe_attr(user, "followers_count", 0),
        "following_count": safe_attr(user, "following_count", 0),
        "statuses_count": safe_attr(user, "statuses_count", 0),
        "verified": bool(safe_attr(user, "verified", False) or safe_attr(user, "is_blue_verified", False)),
    }


def normalize_media(media_items: list[Any] | None) -> list[dict[str, str]]:
    normalized = []
    for media in media_items or []:
        media_type = safe_attr(media, "type", "photo")
        media_url = safe_attr(media, "media_url", "") or safe_attr(media, "url", "")

        # 動画/GIFはプレビューURLを優先し、無ければ最初のストリームURLを使います。
        streams = safe_attr(media, "streams", []) or []
        if not media_url and streams:
            media_url = safe_attr(streams[0], "url", "")

        if media_url:
            normalized.append({"type": media_type, "url": media_url})
    return normalized


def normalize_tweet(tweet: Any) -> dict[str, Any]:
    user = safe_attr(tweet, "user", None)
    retweeted_tweet = safe_attr(tweet, "retweeted_tweet", None)
    display_tweet = retweeted_tweet or tweet
    display_user = safe_attr(display_tweet, "user", user)

    return {
        "id": safe_attr(display_tweet, "id", ""),
        "text": safe_attr(display_tweet, "text", ""),
        "created_at": safe_attr(display_tweet, "created_at", ""),
        "user": normalize_user(display_user),
        "media": normalize_media(safe_attr(display_tweet, "media", [])),
        "reply_count": safe_attr(display_tweet, "reply_count", 0),
        "retweet_count": safe_attr(display_tweet, "retweet_count", 0),
        "favorite_count": safe_attr(display_tweet, "favorite_count", 0),
        "view_count": safe_attr(display_tweet, "view_count", None),
        "is_retweet": retweeted_tweet is not None,
        "quote": normalize_tweet(safe_attr(display_tweet, "quote", None)) if safe_attr(display_tweet, "quote", None) else None,
    }


settings = Settings()
service = TwikitService(settings)
app = FastAPI(title=settings.app_name)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.exception_handler(Exception)
async def app_exception_handler(request: Request, exc: Exception) -> HTMLResponse | JSONResponse:
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": str(exc)}, status_code=500)
    return templates.TemplateResponse(
        "error.html",
        {"request": request, "settings": settings, "message": str(exc)},
        status_code=500,
    )


@app.get("/", response_class=HTMLResponse)
async def home(request: Request, mode: str = Query(default=settings.default_timeline)) -> HTMLResponse:
    tweets = await service.timeline(mode)
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "settings": settings, "tweets": tweets, "mode": mode},
    )


@app.get("/search", response_class=HTMLResponse)
async def search_page(
    request: Request,
    q: str = Query(default=""),
    product: str = Query(default="Latest"),
) -> HTMLResponse:
    tweets = await service.search(q, product) if q else []
    return templates.TemplateResponse(
        "search.html",
        {"request": request, "settings": settings, "tweets": tweets, "q": q, "product": product},
    )


@app.get("/user/{screen_name}", response_class=HTMLResponse)
async def user_page(request: Request, screen_name: str) -> HTMLResponse:
    profile = await service.user_profile(screen_name)
    return templates.TemplateResponse(
        "user.html",
        {"request": request, "settings": settings, **profile},
    )


@app.get("/api/timeline")
async def api_timeline(mode: str = Query(default=settings.default_timeline)) -> dict[str, Any]:
    return {"tweets": await service.timeline(mode)}


@app.get("/api/search")
async def api_search(q: str = Query(..., min_length=1), product: str = Query(default="Latest")) -> dict[str, Any]:
    return {"tweets": await service.search(q, product)}


@app.get("/api/user/{screen_name}")
async def api_user(screen_name: str) -> dict[str, Any]:
    if not screen_name:
        raise HTTPException(status_code=404, detail="User not found")
    return await service.user_profile(screen_name)
