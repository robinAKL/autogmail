"""Keep the tests away from anything real.

A developer's .env may hold live keys (OpenAI, Redis storage, WhatsApp). The app loads
.env on demand, so without this a test run could write test data into the real store.
"""

import os
import re

import dotenv

_LIVE = re.compile(r"KV|REDIS|UPSTASH|WHATSAPP|TWILIO|^MS_|^GMAIL_|^SMTP_|^LIVE_SEND$|^CRON_SECRET$", re.IGNORECASE)

dotenv.load_dotenv = lambda *args, **kwargs: False
for _name in [name for name in os.environ if _LIVE.search(name)]:
    os.environ.pop(_name, None)
