# چک‌لیست استقرار BaleBot روی سرور `rodi` (`185.255.211.219`)

**آخرین بروزرسانی:** دوشنبه ۲۶ اکتبر ۲۰۲۶، ساعت ۱۶:۵۶ (UTC+1 / Europe/Madrid)

---

## ✅ فاز ۰: زیرساخت ایزوله‌سازی Incus (تکمیل‌شده)
- [x] پروژهٔ ایزولهٔ `balebot` در Incus با محدودیت‌ها ساخته شد (`limits.containers=8`, `limits.cpu=8`, `limits.memory=12GiB`).
- [x] بریج اختصاصی `sbxbr0` (`10.77.0.1/24`، فقط IPv4 با NAT) ساخته شد.
- [x] فایروال/ACL شبکهٔ `sbx-egress` روی `sbxbr0` تنظیم و فعال شد (فقط دسترسی به پورت `8001` میزبان برای LLM Proxy، پورت `53` برای DNS و اینترنت عمومی؛ مسدودسازی کامل شبکهٔ داخلی `10.179.96.0/24`، کل رنج‌های RFC1918، پورت‌های `22/8000/8443` میزبان و `169.254.169.254`).
- [x] پروفایل امنیتی `sbx-profile` (`security.nesting=false`, `security.idmap.isolated=true`, `limits.cpu=1`, `limits.memory=1GiB`, `limits.processes=256`, دیسک `8GiB` روی استخر `default`) ساخته شد.
- [x] گواهی TLS محدودشدهٔ کلاینت Incus در `/etc/balebot/incus/` (`client.crt`, `client.key`, `config.yml`, `servercerts/restricted.crt`) ساخته شد و اتصال `incus --project balebot list` تست شد.

---

## ✅ فاز ۱: تکمیل کانتینر الگو (`agent-base`) (تکمیل‌شده در ۲۶ اکتبر ۲۰۲۶، ساعت ۱۴:۱۷ UTC+1)
- [x] کانتینر `agent-base` در پروژهٔ `balebot` ساخته شد و بسته‌های پایه، پایتون ۳.۱۲، Node.js 22، مرورگر و ابزارهای CLI و ریپوی `hermes-agent` (`v0.22.1+163.g046094d`) در `/home/agent/.hermes/hermes-agent` نصب شد.
- [x] بسته‌های Node.js و `npm` در `/home/agent/.hermes/hermes-agent/node_modules` تکمیل شد.
- [x] لینک `/usr/local/bin/hermes` به `/home/agent/.local/bin/hermes` متصل و خروجی `su - agent -c "hermes --version"` تأیید شد.
- [x] فایل‌های کش و هرگونه فایل کلید احتمالی (`.env`, `auth.json`) از داخل `agent-base` پاک‌سازی شد.
- [x] دایرکتوری‌های `/home/agent/in` و `/home/agent/outbox` با مجوز `0700 (agent:agent)` تأیید شدند (حجم نهایی قالب: `~4.7 GiB`؛ سهمیه دیسک هر سندباکس کاربر: `5GiB`).
- [x] کانتینر `agent-base` خاموش (`STOPPED`) شد تا آمادهٔ کلون‌شدن سریع برای کاربران باشد.

---

## ✅ فاز ۲: نصب برنامه روی میزبان، تنظیمات `/etc/balebot/env` و دیتابیس (تکمیل‌شده در ۲۶ اکتبر ۲۰۲۶، ساعت ۱۴:۲۵ UTC+1)
- [x] کاربر و گروه سیستمی `balebot` (`--system --home-dir /var/lib/balebot --shell /usr/sbin/nologin`) ساخته شد.
- [x] کد برنامه و محیط مجازی پایتون (`.venv`) در `/opt/balebot` نصب و مسیرهای اجرایی `.venv/bin/*` اصلاح شد.
- [x] دایرکتوری `/var/lib/balebot` (`0750 balebot:balebot`) و دیتابیس SQLite در `/var/lib/balebot/balebot.db` (`0600 balebot:balebot`) مستقر شد.
- [x] اکانت اولیهٔ پنل مدیریت (`admin` با نقش `superadmin`) در دیتابیس ساخته شد و مشخصات ورود اولیه در `/root/balebot-admin-initial.txt` (`0600 root:root`) ذخیره گردید.
- [x] فایل `/etc/balebot/env` با مجوز `0640 (root:balebot)` ایجاد شد (شامل `BALEBOT_BALE_TOKEN`، `BALEBOT_BASE_MODEL_API_KEY`، کلیدهای تصادفی رمزنگاری `VAULT_KEY` / `SESSION_SECRET` / `PROXY_SECRET` / `WEBHOOK_SECRET`، تنظیم `BALEBOT_SANDBOX_DISK=5GiB`، `BALEBOT_SANDBOX_ENABLED=true` و `BALEBOT_WEBHOOK_BASE_URL=https://node.roditeam.ir`).
- [x] دسترسی گروه `balebot` به `/etc/balebot/incus` (`0750 root:balebot`) تنظیم و اتصال کلاینت Incus با کاربر `balebot` تأیید شد.

---

## ✅ فاز ۳: نصب و راه‌اندازی سرویس‌های Systemd (تکمیل‌شده در ۲۶ اکتبر ۲۰۲۶، ساعت ۱۴:۲۶ UTC+1)
- [x] فایل‌های سرویس (`balebot.service`, `balebot-proxy.service`, `balebot-reap.service`, `balebot-reap.timer`) در `/etc/systemd/system/` نصب و فعال (`enabled & active`) شدند.
- [x] سرویس `balebot-proxy.service` روی `10.77.0.1:8001` فعال است و `GET http://10.77.0.1:8001/healthz` پاسخ `{"status":"ok"}` برمی‌گرداند.
- [x] سرویس `balebot.service` روی پورت `8000` (قابل دسترس از لوکال و گیت‌وی داکر `172.18.0.1:8000` برای `rodo-proxy-1` و مسدود از بیرون و سندباکس‌ها) فعال است، `GET https://node.roditeam.ir/healthz` پاسخ `{"status":"ok"}` و `GET https://node.roditeam.ir/panel/login` کد `200 OK` برمی‌گرداند.
- [x] تایمر `balebot-reap.timer` و اجرای `balebot-reap.service` (`stopped 0, destroyed 0`) بدون خطا تأیید شد.

---

## ✅ فاز ۴: تست امنیتی ایزوله‌سازی (`verify-isolation.sh`) و فعال‌سازی سندباکس (تکمیل‌شده در ۲۶ اکتبر ۲۰۲۶، ساعت ۱۴:۲۹ و تأیید مجدد در ۱۶:۵۳ UTC+1)
- [x] در `verify-isolation.sh` و `SandboxManager.ensure()`، انتظار برای دریافت مسیر پیش‌فرض DHCP IPv4 (`ip -4 route show default | grep -q default` که حدود ۲ ثانیه پس از `incus start` طول می‌کشد) اضافه شد تا درخواست‌های شبکه پیش از دریافت IP ارسال نشوند.
- [x] اسکریپت `/opt/balebot/deploy/verify-isolation.sh` اجرا شد و هر ۱۳ تست امنیتی و شبکه‌ای با موفقیت کامل (`ALL CHECKS PASSED`) تأیید شدند:
  - `PASS  runs as uid 1000, not root`
  - `PASS  cannot read host secrets path`
  - `PASS  template holds no .env`
  - `PASS  cannot mount filesystems`
  - `PASS  cannot write /proc/sys`
  - `PASS  reaches the LLM proxy port`
  - `PASS  host ssh (22) is blocked`
  - `PASS  host port 8000 (bot app) is blocked`
  - `PASS  host incus API (8443) is blocked`
  - `PASS  public internet works (1.1.1.1:443)`
  - `PASS  blocked target 10.179.96.162:22`
  - `PASS  blocked target 192.168.10.196:22`
  - `PASS  metadata address blocked`
- [x] مقدار `BALEBOT_SANDBOX_ENABLED=true` در `/etc/balebot/env` فعال شد و سرویس `balebot.service` ری‌استارت و تأیید سلامت شد.

---

## ✅ فاز ۵: اتصال دامنه `node.roditeam.ir`، گواهی TLS، وب‌هوک بله و جایگزینی بات قدیمی (تکمیل‌شده در ۲۶ اکتبر ۲۰۲۶، ساعت ۱۶:۵۵ UTC+1)
- [x] بکاپ کامل فایل‌های پیکربندی قبلی در `/home/hp/backups/balebot-phase5-20261026-155225/` ذخیره شد.
- [x] گواهی TLS معتبر Let's Encrypt (ECDSA) برای دامنهٔ `node.roditeam.ir` دریافت شد (معتبر تا ۲۴ ژانویه ۲۰۲۷) و اسکریپت تمدید خودکار در `/etc/letsencrypt/renewal-hooks/deploy/balebot-roditeam-to-proxy.sh` نصب گردید.
- [x] بلوک `server` مربوط به `node.roditeam.ir` در `/opt/rodo/deploy/nginx.conf` (`rodo-proxy-1`) اضافه و بدون هیچ قطعی (`nginx -t && nginx -s reload`) بارگذاری شد؛ سلامت هر سه دامنهٔ `https://rodo.n3xus.ir` (`200`)، `https://node.n3xus.ir/healthz` (`ok`) و `https://node.roditeam.ir/healthz` (`{"status":"ok"}`) تأیید شد.
- [x] متغیر `BALEBOT_WEBHOOK_BASE_URL=https://node.roditeam.ir` در `/etc/balebot/env` تنظیم شد.
- [x] سرویس قدیمی تک‌کاربره `bale-bridge.service` داخل کانتینر `hermes` متوقف و غیرفعال (`disabled & inactive`) شد (در حالی که `hermes-gateway.service` برای تلگرام دست‌نخورده و `active` باقی ماند).
- [x] وبهوک بله با دستور `python -m balebot.cli set-webhook` روی `https://node.roditeam.ir/bale/webhook/<secret>` ثبت شد (`webhook set: True`) و از طریق `getWebhookInfo` بله (`pending_update_count: 0`) تأیید گردید.
