import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, func, select

from balebot.models import AdminUser, Message, UsageDaily, User
from balebot.security import hash_password, verify_password
from balebot.services import runtime_settings
from balebot.state import AppState

router = APIRouter(prefix="/panel")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

MIN_PASSWORD = 12
MAX_FAILS, FAIL_WINDOW = 5, 15 * 60
_fails: dict[str, list[float]] = {}  # ip -> failure timestamps (single process)
_DUMMY_HASH = hash_password(secrets.token_hex(8))


def _ctx(request: Request) -> AppState:
    return request.app.state.ctx  # type: ignore[no-any-return]


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse("/panel" + path, status.HTTP_303_SEE_OTHER)


def _csrf(request: Request) -> str:
    return request.session.setdefault("csrf", secrets.token_urlsafe(24))  # type: ignore[no-any-return]


def _render(request: Request, name: str, status_code: int = 200, **data: Any) -> HTMLResponse:
    flash = request.session.pop("flash", None)
    page = {"csrf": _csrf(request), "flash": flash, **data}
    return templates.TemplateResponse(request, name, page, status_code=status_code)


async def check_csrf(request: Request, csrf: Annotated[str, Form()] = "") -> None:
    expected = request.session.get("csrf", "")
    # an empty expected token (no session yet) must never match an empty submitted one
    if not expected or not secrets.compare_digest(csrf, expected):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "bad csrf token")


async def current_admin(request: Request) -> AdminUser:
    admin_id = request.session.get("admin_id")
    admin = None
    if admin_id:
        async with _ctx(request).sessionmaker() as s:
            admin = await s.get(AdminUser, admin_id)
    if admin is None:
        raise HTTPException(status.HTTP_303_SEE_OTHER, headers={"Location": "/panel/login"})
    return admin


async def active_admin(
    request: Request, admin: Annotated[AdminUser, Depends(current_admin)]
) -> AdminUser:
    """Like current_admin, but a pending password change blocks everything else."""
    if admin.must_change_password:
        raise HTTPException(status.HTTP_303_SEE_OTHER, headers={"Location": "/panel/password"})
    return admin


def _too_many_fails(ip: str) -> bool:
    now = time.monotonic()
    recent = [t for t in _fails.get(ip, []) if now - t < FAIL_WINDOW]
    _fails[ip] = recent
    return len(recent) >= MAX_FAILS


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    return _render(request, "login.html")


@router.post("/login", response_model=None)
async def login(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    _: Annotated[None, Depends(check_csrf)],
) -> HTMLResponse | RedirectResponse:
    ip = request.client.host if request.client else "?"
    if _too_many_fails(ip):
        return _render(request, "login.html", 429, error="تلاش‌های ناموفق زیاد است؛ بعداً تلاش کنید.")
    async with _ctx(request).sessionmaker() as s:
        admin = await s.scalar(select(AdminUser).where(AdminUser.username == username))
    ok = verify_password(admin.password_hash if admin else _DUMMY_HASH, password)
    if not (admin and ok):
        _fails.setdefault(ip, []).append(time.monotonic())
        return _render(request, "login.html", 401, error="نام کاربری یا رمز اشتباه است.")
    _fails.pop(ip, None)
    request.session.clear()  # new session on login (no fixation)
    request.session["admin_id"] = admin.id
    return _redirect("/password" if admin.must_change_password else "/")


@router.post("/logout")
async def logout(request: Request, _: Annotated[None, Depends(check_csrf)]) -> RedirectResponse:
    request.session.clear()
    return _redirect("/login")


@router.get("/password", response_class=HTMLResponse)
async def password_page(
    request: Request, admin: Annotated[AdminUser, Depends(current_admin)]
) -> HTMLResponse:
    return _render(request, "password.html", admin=admin, forced=admin.must_change_password)


@router.post("/password", response_model=None)
async def password_change(
    request: Request,
    admin: Annotated[AdminUser, Depends(current_admin)],
    _: Annotated[None, Depends(check_csrf)],
    current: Annotated[str, Form()],
    new: Annotated[str, Form()],
    confirm: Annotated[str, Form()],
) -> HTMLResponse | RedirectResponse:
    error = None
    if not verify_password(admin.password_hash, current):
        error = "رمز فعلی اشتباه است."
    elif len(new) < MIN_PASSWORD:
        error = f"رمز جدید باید حداقل {MIN_PASSWORD} کاراکتر باشد."
    elif new != confirm:
        error = "تکرار رمز یکسان نیست."
    elif new == current:
        error = "رمز جدید باید با رمز فعلی فرق کند."
    if error:
        return _render(
            request,
            "password.html",
            400,
            admin=admin,
            forced=admin.must_change_password,
            error=error,
        )
    async with _ctx(request).sessionmaker() as s:
        row = await s.get(AdminUser, admin.id)
        assert row is not None
        row.password_hash = hash_password(new)
        row.must_change_password = False
        await s.commit()
    request.session.clear()
    request.session["admin_id"] = admin.id
    request.session["flash"] = "رمز تغییر کرد."
    return _redirect("/")


@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request, admin: Annotated[AdminUser, Depends(active_admin)]
) -> HTMLResponse:
    st = _ctx(request)
    today = datetime.now(ZoneInfo(st.settings.timezone)).date()
    async with st.sessionmaker() as s:
        stats = {
            "users": await s.scalar(select(func.count(User.id))),
            "banned": await s.scalar(select(func.count(User.id)).where(User.banned.is_(True))),
            "active_today": await s.scalar(
                select(func.count()).where(UsageDaily.day == today, UsageDaily.count > 0)
            ),
            "requests_today": await s.scalar(
                select(func.coalesce(func.sum(UsageDaily.count), 0)).where(UsageDaily.day == today)
            ),
            "messages": await s.scalar(select(func.count(Message.id))),
        }
        cfg = await runtime_settings.load(s, st.settings)
    return _render(request, "dashboard.html", admin=admin, stats=stats, cfg=cfg, page="dashboard")


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request, admin: Annotated[AdminUser, Depends(active_admin)]
) -> HTMLResponse:
    st = _ctx(request)
    async with st.sessionmaker() as s:
        cfg = await runtime_settings.load(s, st.settings)
    return _render(
        request,
        "settings.html",
        admin=admin,
        cfg=cfg,
        key_set=bool(st.settings.base_model_api_key),
        page="settings",
    )


@router.post("/settings", response_model=None)
async def settings_save(
    request: Request,
    admin: Annotated[AdminUser, Depends(active_admin)],
    _: Annotated[None, Depends(check_csrf)],
    base_url: Annotated[str, Form()],
    base_model: Annotated[str, Form()],
    daily_quota: Annotated[int, Form(ge=0, le=100_000)],
    system_prompt: Annotated[str, Form()] = "",
) -> HTMLResponse | RedirectResponse:
    st = _ctx(request)
    base_url, base_model = base_url.strip(), base_model.strip()
    error = None
    if not base_url.startswith("https://"):
        error = "آدرس base_url باید با https:// شروع شود."
    elif not 1 <= len(base_model) <= 128:
        error = "نام مدل نامعتبر است."
    elif len(system_prompt) > 4000:
        error = "پرامپت سیستم حداکثر ۴۰۰۰ کاراکتر است."
    if error:
        async with st.sessionmaker() as s:
            cfg = await runtime_settings.load(s, st.settings)
        return _render(
            request,
            "settings.html",
            400,
            admin=admin,
            cfg=cfg,
            error=error,
            key_set=bool(st.settings.base_model_api_key),
            page="settings",
        )
    async with st.sessionmaker() as s:
        await runtime_settings.save(
            s,
            {
                "base_url": base_url,
                "base_model": base_model,
                "system_prompt": system_prompt,
                "daily_quota": daily_quota,
            },
        )
        await s.commit()
    request.session["flash"] = "تنظیمات ذخیره شد و همین حالا اعمال می‌شود."
    return _redirect("/settings")


@router.get("/users", response_class=HTMLResponse)
async def users_page(
    request: Request, admin: Annotated[AdminUser, Depends(active_admin)], q: str = ""
) -> HTMLResponse:
    st = _ctx(request)
    today = datetime.now(ZoneInfo(st.settings.timezone)).date()
    query = (
        select(User, func.coalesce(UsageDaily.count, 0))
        .outerjoin(UsageDaily, and_(UsageDaily.user_id == User.id, UsageDaily.day == today))
        .order_by(User.id.desc())
        .limit(100)
    )
    q = q.strip()[:64]
    if q:
        cond = User.username.contains(q, autoescape=True)
        if q.isdigit():
            cond = cond | (User.bale_id == int(q))
        query = query.where(cond)
    async with st.sessionmaker() as s:
        rows = (await s.execute(query)).all()
        cfg = await runtime_settings.load(s, st.settings)
    return _render(
        request,
        "users.html",
        admin=admin,
        rows=rows,
        q=q,
        default_quota=cfg.daily_quota,
        page="users",
    )


@router.post("/users/{user_id}/ban")
async def user_ban(
    user_id: int,
    request: Request,
    _admin: Annotated[AdminUser, Depends(active_admin)],
    _: Annotated[None, Depends(check_csrf)],
) -> RedirectResponse:
    async with _ctx(request).sessionmaker() as s:
        user = await s.get(User, user_id)
        if user is None:
            raise HTTPException(404)
        user.banned = not user.banned
        await s.commit()
    return _redirect("/users")


@router.post("/users/{user_id}/quota")
async def user_quota(
    user_id: int,
    request: Request,
    _admin: Annotated[AdminUser, Depends(active_admin)],
    _: Annotated[None, Depends(check_csrf)],
    quota: Annotated[str, Form()] = "",
) -> RedirectResponse:
    value: int | None = None
    if quota.strip():
        if not quota.strip().isdigit() or int(quota) > 100_000:
            request.session["flash"] = "سهمیه باید عدد بین ۰ تا ۱۰۰۰۰۰ باشد."
            return _redirect("/users")
        value = int(quota)
    async with _ctx(request).sessionmaker() as s:
        user = await s.get(User, user_id)
        if user is None:
            raise HTTPException(404)
        user.daily_quota = value  # empty = back to the default
        await s.commit()
    return _redirect("/users")
