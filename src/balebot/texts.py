"""User-facing Persian strings and interactive button keyboards."""

WELCOME = (
    "سلام! 👋 به ربات هوشمند هرمس خوش آمدید.\n"
    "هر روز {limit} درخواست رایگان دارید (امروز {remaining} باقی مانده).\n"
    "هر سوالی دارید همین‌جا بپرسید، یا از دکمه‌های زیر استفاده کنید:\n\n"
    "💬 پیام معمولی — گفتگوی مستقیم با هوش مصنوعی\n"
    "🛠 /agent — اجرای ابزارها و کد در محیط لینوکس اختصاصی شما\n"
    "🤖 /models — مشاهده و تغییر مدل هوش مصنوعی\n"
    "🔑 /addkey — اتصال کلید شخصی (بدون محدودیت سهمیه)\n"
    "📊 /usage — مشاهده مصرف امروز\n"
    "🔄 /new — شروع گفتگوی تازه\n"
    "📖 /help — راهنمای کامل استفاده از ربات"
)

MENU_TEXT = (
    "📋 منوی اصلی ربات هرمس\n\nیکی از گزینه‌های زیر را لمس کنید یا مستقیم پیام خود را بنویسید:"
)

HELP = (
    "📖 راهنمای کامل استفاده از ربات هوشمند هرمس\n\n"
    "۱) 💬 گفتگوی هوش مصنوعی (چت معمولی):\n"
    "کافی است سوال، متن، یا کد خود را مستقیماً در چت بنویسید. "
    "ربات تاریخچه‌ی گفتگو را به خاطر می‌سپارد.\n"
    "• برای پاک کردن حافظه‌ی گفتگو و شروع موضوع جدید، دکمه‌ی "
    "«🔄 گفتگوی تازه» یا دستور /new را بزنید.\n\n"
    "۲) 🛠 اجرای ایجنت و ابزارها در محیط ایزوله (/agent):\n"
    "شما یک کانتینر لینوکس اختصاصی و ایزوله دارید! با دستور /agent یا دکمه‌ی "
    "«🛠 اجرای ایجنت هرمس» می‌توانید کارهایی که نیاز به نوشتن و اجرای کد پایتون/بش، "
    "پردازش داده یا ساخت فایل دارند را به ایجنت بسپارید.\n"
    "• مثال ۱: `/agent یک اسکریپت پایتون بنویس که ۲۰ عدد اول فیبوناچی را حساب کند`\n"
    "• مثال ۲: `/agent یک فایل CSV نمونه از فروش ماهانه در پوشه out بساز`\n"
    "• نکته: هر فایل خروجی که ایجنت در پوشه‌ی `/home/agent/out` ذخیره کند، "
    "خودکار در بله برای شما ارسال می‌شود!\n"
    "• هزینه: روی مدل رایگان پایه ۵ واحد از سهمیه روزانه کسر می‌شود "
    "(روی کلید شخصی کاملاً رایگان است).\n\n"
    "۳) 🔑 ثبت کلید API شخصی (استفاده نامحدود و رایگان از سهمیه):\n"
    "با زدن دکمه‌ی «🔑 افزودن کلید API» یا دستور /addkey می‌توانید کلید خودتان "
    "از سرویس‌های OpenAI، OpenRouter، Groq، DeepSeek، Z.ai، Gemini یا هر سرویس "
    "سازگار با OpenAI را ثبت کنید.\n"
    "• پیام حاوی کلید شما بلافاصله از چت حذف شده و با رمزنگاری AES-256-GCM "
    "در گاوصندوق ذخیره می‌شود.\n"
    "• مدیریت کلیدها: /keys | حذف کلید: `/delkey شماره` | "
    "افزودن دستی مدل: `/addmodel شماره نام-مدل`\n\n"
    "۴) 🤖 تغییر مدل هوش مصنوعی (/models):\n"
    "با زدن «🤖 انتخاب مدل» یا دستور /models می‌توانید بین مدل پایه رایگان "
    "و مدل‌های کلید شخصی خودتان جابجا شوید.\n\n"
    "۵) 📊 سهمیه روزانه (/usage):\n"
    "با زدن «📊 سهمیه امروز» یا دستور /usage تعداد درخواست‌های مصرف‌شده "
    "و باقی‌مانده امروز را ببینید."
)

AGENT_PROMPT = (
    "🛠 حالت اجرای ایجنت هرمس (محیط لینوکس اختصاصی)\n\n"
    "لطفاً کاری که می‌خواهید ایجنت در محیط ایزوله‌ی شما انجام دهد را در پیام بعدی بنویسید "
    "(یا مستقیماً از دستور `/agent <شرح کار>` استفاده کنید).\n\n"
    "💡 نمونه‌ها:\n"
    "• یک اسکریپت پایتون بنویس که اعداد اول زیر ۱۰۰ را پیدا کند و اجرا کن\n"
    "• یک فایل گزارش متنی در پوشه out بساز تا برایم ارسال شود\n\n"
    "برای انصراف و بازگشت به چت عادی، دکمه‌ی زیر یا /cancel را بزنید."
)

USAGE = "مصرف امروز: {used} از {limit} (باقی‌مانده: {remaining})"
QUOTA_EXHAUSTED = (
    "سهمیه‌ی رایگان امروز تمام شد. فردا دوباره ریست می‌شود (یا با /addkey کلید شخصی ثبت کنید)."
)
NEW_CHAT = "گفتگوی تازه شروع شد."
BANNED = "دسترسی شما مسدود شده است."
MODEL_ERROR = "پاسخ‌گویی مدل موقتاً ناموفق بود. این درخواست از سهمیه‌ی شما کم نشد، دوباره تلاش کنید."
UNSUPPORTED = "فعلاً فقط پیام متنی، تصویر، ویس، فایل صوتی و سند پشتیبانی می‌شود."
TOO_LONG = "پیام خیلی طولانی است (حداکثر {max} کاراکتر)."

# ---- personal keys ----
KEYS_OFF = "ثبت کلید شخصی فعلاً در این ربات فعال نیست."
ASK_SITE = (
    "کلید را از کدام سایت می‌دهید؟\n"
    "یکی از سرویس‌های زیر را انتخاب کنید، یا نام/آدرس سایت و مستندات API آن را بفرستید.\n"
    "برای انصراف: /cancel"
)
SITE_UNKNOWN = (
    "این سرویس را نمی‌شناسم. آدرس سایت یا مستندات API را بفرستید "
    "(مثل https://example.com) یا /cancel بزنید."
)
AI_FAILED = (
    "نتوانستم تنظیمات اتصال این سرویس را از مستنداتش بسازم "
    "(شاید سازگار با OpenAI نیست). این درخواست از سهمیه‌ی شما کم نشد."
)
CONFIRM = (
    "اتصال پیشنهادی:\nسرویس: {name}\nآدرس API: {host}\n\n"
    "کلید شما فقط به همین آدرس فرستاده می‌شود. آدرس درست است؟ (بله / خیر)"
)
ASK_KEY = (
    "کلید API را بفرستید. پیام شما بلافاصله از گفتگو پاک می‌شود و کلید فقط رمزنگاری‌شده "
    "نگهداری می‌شود و فقط برای خودتان استفاده می‌شود.\nبرای انصراف: /cancel"
)
KEY_BAD_FORMAT = "این شبیه یک کلید نیست. فقط خود کلید را (بدون فاصله) بفرستید یا /cancel بزنید."
KEY_REJECTED = "سرویس این کلید را نپذیرفت (کلید نامعتبر یا بدون دسترسی). دوباره بفرستید یا /cancel."
KEY_PROBE_FAILED = "ارتباط با سرویس برقرار نشد. کمی بعد دوباره امتحان کنید."
KEY_SAVED = "✅ کلید «{name}» ثبت شد. {count} مدل پیدا شد.\nبرای انتخاب مدل: /models"
KEY_SAVED_NO_MODELS = (
    "✅ کلید «{name}» ثبت شد ولی فهرست مدل‌ها در دسترس نبود.\n"
    "مدل را دستی اضافه کنید: /addmodel {id} نام-مدل"
)
CANCELLED = "انصراف داده شد."
NO_KEYS = "هنوز کلیدی ثبت نکرده‌اید. /addkey"
KEYS_HEADER = "کلیدهای شما:"
KEY_DELETED = "کلید و مدل‌های آن حذف شد."
KEY_NOT_FOUND = "کلیدی با این شماره پیدا نشد."
MODELS_HEADER = "مدل‌های در دسترس (برای انتخاب: /model شماره):"
MODEL_SET = "مدل فعال: {label}"
MODEL_BAD = "شماره‌ی مدل نامعتبر است. /models"
ADDMODEL_USAGE = "مثال: /addmodel 3 gpt-4o-mini"
MODEL_ADDED = "مدل اضافه شد."
BASE_LABEL = "مدل پایه ({model}) — با سهمیه‌ی رایگان"

# ---- agent sandbox ----
AGENT_OFF = "اجرای ابزارها (/agent) فعلاً فعال نیست."
AGENT_USAGE = (
    "کار را بعد از دستور بنویسید. مثال: /agent یک اسکریپت پایتون بنویس که فایل CSV را خلاصه کند"
)
AGENT_BUSY = "کار قبلی شما هنوز در حال اجراست. کمی صبر کنید."
AGENT_RUNNING = "⏳ در حال اجرا در محیط ایزوله... (ممکن است چند دقیقه طول بکشد)"
AGENT_NO_QUOTA = "برای این کار {cost} واحد از سهمیه‌ی روزانه لازم است و باقی‌مانده کافی نیست."
AGENT_FAILED = "اجرای کار ناموفق بود. این کار از سهمیه‌ی شما کم نشد."
AGENT_TIMEOUT = "زمان اجرا تمام شد و کار نیمه‌کاره ماند. کار را کوچک‌تر کنید و دوباره تلاش کنید."
AGENT_EMPTY = "(بدون خروجی متنی)"

KEYBOARD_READY = "⌨️ منوی دسترسی سریع پایین صفحه آماده است."
STATUS_INFO = (
    "📡 وضعیت ربات و محیط هرمس:\n"
    "• وضعیت سرور: 🟢 فعال و آماده\n"
    "• محیط لینوکس اختصاصی: 🟢 فعال (Hermes Sandbox)\n"
    "• مدل انتخابی: {model}\n"
    "• {usage}"
)

# ---- bot commands menu ----
BOT_COMMANDS: list[dict[str, str]] = [
    {"command": "new", "description": "شروع گفتگوی تازه و پاک کردن حافظه"},
    {"command": "menu", "description": "نمایش منوی اصلی و امکانات ربات"},
    {"command": "models", "description": "مشاهده و تغییر مدل هوش مصنوعی"},
    {"command": "keys", "description": "مدیریت و ثبت کلید API شخصی"},
    {"command": "usage", "description": "مشاهده سهمیه و مصرف امروز"},
    {"command": "help", "description": "راهنمای کامل کار با دستیار هرمس"},
]

# ---- bottom reply keyboard ----
BOTTOM_REPLY_KEYBOARD: list[list[dict[str, str]]] = [
    [{"text": "🔄 گفتگوی تازه"}, {"text": "📋 منوی اصلی"}],
    [{"text": "🤖 انتخاب مدل"}, {"text": "🔑 کلیدهای من"}],
    [{"text": "📊 سهمیه امروز"}, {"text": "📖 راهنما"}],
]

# ---- inline keyboards ----
MAIN_MENU_KEYBOARD: list[list[dict[str, str]]] = [
    [
        {"text": "🛠 اجرای ایجنت هرمس", "callback_data": "menu:agent"},
        {"text": "🤖 انتخاب مدل", "callback_data": "menu:models"},
    ],
    [
        {"text": "📊 سهمیه امروز", "callback_data": "menu:usage"},
        {"text": "🔄 گفتگوی تازه", "callback_data": "menu:new"},
    ],
    [
        {"text": "🔑 افزودن کلید API", "callback_data": "menu:addkey"},
        {"text": "🗂 کلیدهای من", "callback_data": "menu:keys"},
    ],
    [
        {"text": "📖 راهنمای کامل ربات", "callback_data": "menu:help"},
    ],
]

BACK_TO_MENU_KEYBOARD: list[list[dict[str, str]]] = [
    [
        {"text": "📋 منوی اصلی", "callback_data": "menu:main"},
        {"text": "📖 راهنما", "callback_data": "menu:help"},
    ]
]

CANCEL_KEYBOARD: list[list[dict[str, str]]] = [
    [{"text": "❌ انصراف", "callback_data": "menu:cancel"}]
]

SITE_PICK_KEYBOARD: list[list[dict[str, str]]] = [
    [
        {"text": "OpenRouter", "callback_data": "site:openrouter"},
        {"text": "OpenAI", "callback_data": "site:openai"},
    ],
    [
        {"text": "DeepSeek", "callback_data": "site:deepseek"},
        {"text": "Groq", "callback_data": "site:groq"},
    ],
    [
        {"text": "Gemini", "callback_data": "site:gemini"},
        {"text": "Z.ai", "callback_data": "site:z.ai"},
    ],
    [{"text": "❌ انصراف", "callback_data": "menu:cancel"}],
]

CONFIRM_KEYBOARD: list[list[dict[str, str]]] = [
    [
        {"text": "✅ بله، تایید", "callback_data": "confirm:yes"},
        {"text": "❌ خیر، انصراف", "callback_data": "confirm:no"},
    ]
]

KEYS_ACTION_KEYBOARD: list[list[dict[str, str]]] = [
    [
        {"text": "🔑 افزودن کلید جدید", "callback_data": "menu:addkey"},
        {"text": "🤖 انتخاب مدل", "callback_data": "menu:models"},
    ],
    [{"text": "📋 منوی اصلی", "callback_data": "menu:main"}],
]

# ---- hermes agent environment ----
HERMES_AGENTS_MD = """# Hermes Agent Environment in Bale Messenger

You are running inside an isolated Linux sandbox connected directly to
the Iranian messenger Bale (@hermes_bot).
- Your direct text replies go straight to the user in this Bale chat.
- Never ask "Which platform or messenger should I send this to?" — you are in Bale.
- Files sent by the user (photos, voice, audio, documents) are in `/home/agent/in/`.
- Files placed in `/home/agent/out/` are automatically sent to the user in Bale.
- Persistent files across sessions must be saved in `/home/agent/workspace/`.
- ALL built-in tools work via the internal proxy WITHOUT external API keys:
  1. `web_search` and `web_extract` work via the internal proxy (no API keys needed).
     CLI: `/home/agent/.local/bin/web-search "<query>"` and `web-extract "<url>"`.
  2. `vision_analyze` works via the internal proxy (`glm-4.6v-flash`) on images.
  3. `text_to_speech` generates Persian/English MP3 audio and copies it to
     `/home/agent/out/` for Bale delivery.
     CLI: `/home/agent/.local/bin/balebot-tts` and `balebot-stt`.
- For immediate or scheduled notifications (e.g. via crontab) to the user in Bale:
  `/home/agent/.local/bin/bale-send "<text>"`
"""

HERMES_BALE_SYSTEM_PROMPT = (
    "تو یک دستیار هوشمند و کارآمد در پیام‌رسان ایرانی «بله» (Bale) به آیدی @hermes_bot هستی. "
    "پاسخ متنی تو مستقیماً در این چت بله برای کاربر ارسال می‌شود. "
    "هرگز نپرس «پیام را در کدام پلتفرم یا پیام‌رسان بفرستم؟» چون همیشه در بله هستی. "
    "هر فایلی که کاربر بفرستد در `/home/agent/in/` قرار می‌گیرد و هر فایلی که در پوشه "
    "`/home/agent/out/` قرار دهی خودکار برای کاربر در بله ارسال می‌شود. "
    "فایل‌های ماندگار و دائمی را در `/home/agent/workspace/` ذخیره کن. "
    "همه ابزارهای تو شامل `web_search`، `web_extract`، `vision_analyze` (بینایی ماشین) "
    "و `text_to_speech` (تبدیل متن به گفتار) از طریق پراکسی داخلی بدون نیاز به هیچ کلید "
    "API خارجی (مثل FIRECRAWL_API_KEY یا KEENABLE_API_KEY) کاملاً فعال هستند. "
    "همچنین ابزارهای خط فرمان `/home/agent/.local/bin/web-search`، "
    "`/home/agent/.local/bin/web-extract`، `/home/agent/.local/bin/balebot-tts`، "
    "`/home/agent/.local/bin/balebot-stt` و `/home/agent/.local/bin/bale-send` در دسترس‌اند. "
    "همیشه نام پیام‌رسان را «بله» بنویس (هرگز «بیل» ننویس)."
)
