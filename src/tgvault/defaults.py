"""Default Telegram app credentials.

These are the PUBLIC api_id/api_hash published in the source code of
Telegram Desktop (https://github.com/telegramdesktop/tdesktop). They identify
the *application*, not the user — your login is still your own QR-authorized
session. Telegram's ToS prefers that each developer registers their own pair
at https://my.telegram.org, but that page frequently refuses new apps with an
unexplained ERROR, so for personal use the published pair is the pragmatic
default. Override any time with TGVAULT_API_ID / TGVAULT_API_HASH or
`tgvault login --api-id ... --api-hash ...`.
"""

# From tdesktop source (SDK/telegram_api documentation); public knowledge.
DEFAULT_API_ID = 2040
DEFAULT_API_HASH = "b18441a1ff607e10a989891a5462e627"
