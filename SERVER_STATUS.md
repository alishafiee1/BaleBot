# گزارش نهایی وضعیت سرور `rodi` و استقرار کامل پروژه BaleBot

**تاریخ تکمیل استقرار:** دوشنبه ۲۶ اکتبر ۲۰۲۶، ساعت ۱۶:۵۶ به وقت مادرید (`UTC+1` / `Europe/Madrid`)
**سرور میزبان (`rodi`):** `185.255.211.219` (Ubuntu 24.04.2 LTS, Kernel 6.8.0-71-generic, 8 vCPU, 15 GiB RAM, 193 GiB Disk)
**دامنهٔ فعال BaleBot:** `https://node.roditeam.ir`

---

## ۱. خلاصه مدیریتی (تمام فازهای ۰ تا ۵ به صورت ۱۰۰٪ تکمیل شدند ✅)

استقرار نسخهٔ جدید چندکاربره و ایزولهٔ **BaleBot** روی سرور `rodi` در تمام ۵ فاز با موفقیت کامل انجام شد و بات جدید اکنون به صورت رسمی روی دامنهٔ **`https://node.roditeam.ir`** فعال است:

1. **زیرساخت ایزوله و کانتینر الگوی `agent-base` (فازهای ۰ و ۱):** پروژهٔ Incus به نام `balebot`، بریج `sbxbr0` (`10.77.0.1/24`)، قوانین فایروال `sbx-egress` و کانتینر الگوی `agent-base` (`Hermes Agent v0.22.1+163.g046094d`) آماده و تست شده است.
2. **نصب برنامه، دیتابیس و تنظیمات امنیتی (فازهای ۲ و ۳):**
   - کد و محیط مجازی در `/opt/balebot` و دیتابیس SQLite در `/var/lib/balebot/balebot.db` (`0600 balebot:balebot`) مستقر شدند.
   - فایل `/etc/balebot/env` (`0640 root:balebot`) با کلیدهای رمزنگاری، توکن بات بله، کلید API مدل پایه، `BALEBOT_SANDBOX_ENABLED=true` و `BALEBOT_WEBHOOK_BASE_URL=https://node.roditeam.ir` پیکربندی شد.
   - اکانت اولیهٔ ادمین (`admin`) ساخته شد و مشخصات ورود آن در `/root/balebot-admin-initial.txt` (`0600 root:root`) روی سرور ذخیره گردید.
3. **تست امنیتی ایزوله‌سازی (فاز ۴):**
   - اسکریپت `/opt/balebot/deploy/verify-isolation.sh` هر ۱۳ بررسی امنیتی و فایروال (از جمله مسدود بودن پورت‌های `22`، `8000` و `8443` میزبان از داخل سندباکس) را با نتیجهٔ **`ALL CHECKS PASSED`** پشت سر گذاشت.
4. **گواهی TLS، پروکسی Nginx و سوییچ نهایی وبهوک بله (فاز ۵):**
   - بکاپ پیکربندی‌های قبلی در `/home/hp/backups/balebot-phase5-20261026-155225/` ذخیره شد.
   - گواهی رسمی Let's Encrypt برای `node.roditeam.ir` دریافت و قلاب تمدید خودکار در `/etc/letsencrypt/renewal-hooks/deploy/balebot-roditeam-to-proxy.sh` تنظیم شد.
   - بلوک HTTPS دامنهٔ `node.roditeam.ir` به `/opt/rodo/deploy/nginx.conf` اضافه و بدون قطعی در `rodo-proxy-1` بارگذاری شد.
   - سرویس قدیمی `bale-bridge.service` در کانتینر `hermes` متوقف و غیرفعال شد (سرویس `hermes-gateway.service` دست‌نخورده و فعال است).
   - وبهوک بله روی `https://node.roditeam.ir/bale/webhook/<secret>` ثبت و با `getWebhookInfo` بله تأیید شد.

---

## ۲. وضعیت سرویس‌های در حال اجرا روی `rodi`

| سرویس / کانتینر | وضعیت | پورت / آدرس | توضیحات |
|---|---|---|---|
| `balebot.service` | `active (running)` | `https://node.roditeam.ir` (`127.0.0.1:8000` / `172.18.0.1:8000`) | وب‌اپلیکیشن اصلی BaleBot (وبهوک بله + پنل ادمین) با `BALEBOT_SANDBOX_ENABLED=true` |
| `balebot-proxy.service` | `active (running)` | `10.77.0.1:8001` | پروکسی LLM برای کانتینرهای سندباکس روی بریج `sbxbr0` |
| `balebot-reap.timer` | `active (waiting)` | هر ۵ دقیقه | جمع‌آوری و توقف کانتینرهای سندباکس غیرفعال |
| `agent-base` (Incus `balebot`) | `STOPPED` | — | کانتینر الگوی آماده برای کلون شدن به ازای هر کاربر (`sbx-u<id>`) |
| `hermes` (Incus `default`) | `RUNNING` | `10.179.96.162` | `hermes-gateway.service` فعال (`active`)؛ سرویس قدیمی `bale-bridge.service` متوقف و غیرفعال (`inactive/disabled`) |
| `rodo-proxy-1` و کانتینرهای RODO | `Up (healthy)` | `80`, `443` | سرویس‌دهی هم‌زمان و سالم به `rodo.n3xus.ir`، `node.n3xus.ir` و `node.roditeam.ir` |
| `csbroker.service` | `active (running)` | `https://node.n3xus.ir` (`172.18.0.1:8088`) | کاملاً دست‌نخورده و سالم |

---

## ۳. آدرس‌های دسترسی و مدیریت

- **پنل مدیریت وب BaleBot:** `https://node.roditeam.ir/panel/login`
  - مشخصات ورود اولیهٔ کاربر `admin` در فایل `/root/balebot-admin-initial.txt` روی سرور `rodi` قرار دارد.
- **بررسی سلامت سرویس:** `https://node.roditeam.ir/healthz` -> `{"status":"ok"}`
- **وبهوک ثبت‌شده در بله:** `https://node.roditeam.ir/bale/webhook/<secret>` (`pending_update_count: 0`)

---

## ۴. بروزرسانی‌های ۲۶–۲۷ اکتبر ۲۰۲۶ (پایداری `/agent`، منوی بات و همگام‌سازی کامل)

- **منوی تعاملی و تجربهٔ کاربری بله (`texts.py`, `services/chat.py`, `services/keys.py`, `gateway/updates.py`):**
  - افزودن کیبورد شیشه‌ای اصلی (`MAIN_MENU_KEYBOARD`) و دستور `/menu` و دکمهٔ `🛠 اجرای ایجنت هرمس` (`menu:agent`).
  - اصلاح پرامپت سیستم پیش‌فرض (`config.py`) به فارسی روان با تأکید بر نوشتن نام پیام‌رسان به صورت «بله».
- **رفع کامل خطای Timeout در `/agent` و پروکسی LLM (`sandbox/proxy.py`, `services/model_client.py`, `net/safe_http.py`, `sandbox/manager.py`):**
  - اولویت‌دهی به مدل فعال `glm-4.7-flash` با `thinking={"type": "disabled"}` در `config.py`، `/etc/balebot/env`، دیتابیس `balebot.db` و لیست fallbackهای Z.ai.
  - پشتیبانی از تبدیل پاسخ‌های غیرجریانی به استریم SSE (`to_sse`) در `proxy.py` برای سازگاری کامل با کلاینت استریم Hermes.
  - افزودن قابلیت Failover خودکار بین IPهای Anycast در `SafeHttp.request()` در صورت بروز Timeout روی یکی از نودهای Z.ai.
  - مدیریت استثنای `UnsafeURL` و کدهای خطای `429/5xx` داخل حلقهٔ کاندیدهای `proxy.py` با تایم‌اوت ۴۵ ثانیه به ازای هر تلاش.
  - تنظیم `agent_toolsets = "terminal,file,web"` و پاک‌سازی پروسه‌های پس‌زمینهٔ `hermes_cli` در صورت بروز Timeout.
