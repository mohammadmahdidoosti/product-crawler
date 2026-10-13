# کراولر محصولات

سرویس وب برای جمع‌آوری محصولات سایت‌های فروشگاهی (ووکامرس و سایت‌های عمومی). آدرس سایت را وارد می‌کنید و خروجی Excel / CSV / JSON تحویل می‌گیرید.

## فایل‌ها

| فایل | کار |
|---|---|
| `app.py` | سرور وب و API (فقط کتابخانه‌ی استاندارد پایتون) |
| `static/index.html` | صفحه‌ی فارسی |
| `crawler.py` | موتور کراول؛ از خط فرمان هم قابل اجراست |
| `render.yaml` | تنظیمات دیپلوی روی Render |

## دیپلوی روی Render

1. این پوشه را در یک ریپوی GitHub قرار دهید.
2. در Render: **New → Blueprint** و ریپو را انتخاب کنید. Render خودش `render.yaml` را می‌خواند.
3. مقدار `APP_PASSWORD` را بخواهد، یک رمز بگذارید.
4. بعد از دیپلوی، آدرس `https://<نام>.onrender.com` را باز کنید. نام کاربری هر چیزی می‌تواند باشد و رمز همان `APP_PASSWORD` است.

## متغیرهای محیطی

| متغیر | پیش‌فرض | توضیح |
|---|---|---|
| `APP_PASSWORD` | خالی (بدون رمز) | رمز صفحه و API |
| `MAX_CONCURRENT_JOBS` | `2` | تعداد کراول هم‌زمان |
| `CRAWLER_PROXY` | — | پروکسی برای سایت‌هایی که IP خارجی را می‌بندند |
| `DATA_DIR` | `/tmp/crawler-jobs` | محل ذخیره‌ی خروجی‌ها |

## API

```bash
# شروع کراول
curl -u x:PASS -X POST https://<app>.onrender.com/api/jobs \
     -d '{"url":"https://tsaco-diesel.com","mode":"auto","max_pages":2000,"delay":1}'
# وضعیت
curl -u x:PASS https://<app>.onrender.com/api/jobs/<id>
# دانلود
curl -u x:PASS -o out.xlsx https://<app>.onrender.com/api/jobs/<id>/download/xlsx
```

## روش کار کراولر

برای هر سایت به ترتیب امتحان می‌کند:

1. **API ووکامرس** (`/wp-json/wc/store/v1/products`) — سریع‌ترین
2. **سایت‌مپ** محصولات و خواندن صفحه‌ها
3. **پیمایش لینک‌ها** از صفحه‌ی اصلی

اطلاعات از JSON-LD، OpenGraph و HTML ووکامرس خوانده می‌شود. robots.txt رعایت می‌شود.

## نکات پلن رایگان Render

- بعد از ۱۵ دقیقه بدون درخواست می‌خوابد. وقتی صفحه باز است بیدار می‌ماند، پس صفحه را تا پایان کراول باز نگه دارید.
- با هر ری‌استارت یا دیپلوی، خروجی‌ها پاک می‌شوند. فایل را همان موقع دانلود کنید.

## اجرای محلی

```bash
pip install -r requirements.txt
APP_PASSWORD=test python app.py        # http://localhost:8000
python crawler.py --sites https://example.com/   # فقط خط فرمان
```
