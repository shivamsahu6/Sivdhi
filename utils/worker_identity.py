"""Worker identity card and per-booking customer verification helpers."""
from __future__ import annotations

import base64
import html
import mimetypes
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKER_UPLOAD_DIR = PROJECT_ROOT / "static" / "uploads"


def ensure_worker_id_verification_schema(conn) -> None:
    """Create the additive audit table used to gate service start."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS booking_worker_id_verifications (
            booking_id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL,
            worker_id INTEGER NOT NULL,
            verified_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_worker_id_verification_owner
        ON booking_worker_id_verifications(customer_id, booking_id)
        """
    )


def is_worker_id_verified(conn, booking_id, customer_id, worker_id) -> bool:
    """True only when this customer verified this exact assigned worker."""
    if not booking_id or not customer_id or not worker_id:
        return False
    ensure_worker_id_verification_schema(conn)
    row = conn.execute(
        """
        SELECT 1
        FROM booking_worker_id_verifications
        WHERE booking_id=?
          AND customer_id=?
          AND worker_id=?
        LIMIT 1
        """,
        (int(booking_id), int(customer_id), int(worker_id)),
    ).fetchone()
    return row is not None


def _photo_data_uri(filename) -> str:
    """Embed only an image file inside Sivdhi's managed upload directory."""
    raw = str(filename or "").strip()
    if not raw or Path(raw).name != raw:
        return ""

    path = (WORKER_UPLOAD_DIR / raw).resolve()
    try:
        if path.parent != WORKER_UPLOAD_DIR.resolve() or not path.is_file():
            return ""
        mime = mimetypes.guess_type(path.name)[0] or ""
        if mime not in {"image/jpeg", "image/png", "image/webp"}:
            return ""
        data = path.read_bytes()
        if not data or len(data) > 5 * 1024 * 1024:
            return ""
        return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")
    except OSError:
        return ""


def build_worker_id_card_svg(worker) -> str:
    """Build a self-contained, downloadable SVG Worker ID card."""
    if not worker:
        raise ValueError("Worker record is required.")

    keys = set(worker.keys()) if hasattr(worker, "keys") else set(worker)
    def value(key, fallback=""):
        return worker[key] if key in keys and worker[key] is not None else fallback

    fullname = html.escape(str(value("fullname", "Sivdhi Worker")).strip()[:90])
    worker_id = html.escape(str(value("worker_id", "")).strip()[:40])
    skills_raw = str(value("skills", "") or "")
    skills = [
        html.escape(part.strip()[:32])
        for part in skills_raw.replace("|", ",").split(",")
        if part.strip()
    ][:3]
    skills_text = "  •  ".join(skills) if skills else "Home Service Professional"
    photo_uri = _photo_data_uri(value("profile_photo", ""))
    photo_svg = (
        f'<image href="{photo_uri}" x="68" y="158" width="190" height="190" '
        'preserveAspectRatio="xMidYMid slice" clip-path="url(#photoClip)"/>'
        if photo_uri
        else '<text x="163" y="278" text-anchor="middle" font-size="104" fill="#6d28d9">👷</text>'
    )

    return f'''<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"
        width="900" height="560" viewBox="0 0 900 560" role="img"
        aria-labelledby="cardTitle cardDescription">
  <title id="cardTitle">Sivdhi Worker ID Card - {fullname}</title>
  <desc id="cardDescription">Official Sivdhi Worker ID. Worker ID {worker_id}. Show this card to customers at every service visit.</desc>
  <defs>
    <linearGradient id="cardBg" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#201039"/>
      <stop offset="58%" stop-color="#432078"/>
      <stop offset="100%" stop-color="#17142f"/>
    </linearGradient>
    <linearGradient id="accent" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="#d946ef"/>
      <stop offset="100%" stop-color="#7c3aed"/>
    </linearGradient>
    <clipPath id="photoClip"><rect x="68" y="158" width="190" height="190" rx="28"/></clipPath>
  </defs>
  <rect x="8" y="8" width="884" height="544" rx="42" fill="url(#cardBg)"/>
  <rect x="8" y="8" width="884" height="544" rx="42" fill="none" stroke="#b889f5" stroke-opacity=".62" stroke-width="3"/>
  <path d="M8 50 Q8 8 50 8 H850 Q892 8 892 50 V124 H8 Z" fill="#ffffff" fill-opacity=".08"/>
  <rect x="48" y="48" width="7" height="55" rx="4" fill="url(#accent)"/>
  <text x="78" y="82" fill="#ffffff" font-family="Arial,sans-serif" font-size="37" font-weight="800" letter-spacing="1">SIVDHI</text>
  <text x="80" y="108" fill="#e7d7ff" font-family="Arial,sans-serif" font-size="14" font-weight="700" letter-spacing="4">WORKER ID CARD</text>
  <rect x="68" y="158" width="190" height="190" rx="28" fill="#ffffff" fill-opacity=".94"/>
  {photo_svg}
  <rect x="68" y="158" width="190" height="190" rx="28" fill="none" stroke="#ffffff" stroke-width="5"/>
  <text x="300" y="184" fill="#d9c5ff" font-family="Arial,sans-serif" font-size="17" font-weight="700" letter-spacing="2">WORKER NAME</text>
  <text x="300" y="232" fill="#ffffff" font-family="Arial,sans-serif" font-size="37" font-weight="800">{fullname}</text>
  <text x="300" y="296" fill="#d9c5ff" font-family="Arial,sans-serif" font-size="17" font-weight="700" letter-spacing="2">WORKER ID</text>
  <text x="300" y="340" fill="#ffffff" font-family="Arial,sans-serif" font-size="34" font-weight="800" letter-spacing="1">{worker_id}</text>
  <text x="68" y="406" fill="#d9c5ff" font-family="Arial,sans-serif" font-size="16" font-weight="700" letter-spacing="2">SERVICE SKILLS</text>
  <text x="68" y="440" fill="#ffffff" font-family="Arial,sans-serif" font-size="22" font-weight="600">{skills_text}</text>
  <rect x="68" y="474" width="764" height="2" fill="#ffffff" fill-opacity=".2"/>
  <text x="68" y="512" fill="#ffffff" font-family="Arial,sans-serif" font-size="18" font-weight="700">Show this card to the customer at every service visit.</text>
  <text x="832" y="512" text-anchor="end" fill="#d9c5ff" font-family="Arial,sans-serif" font-size="13">SIVDHI</text>
</svg>'''


def worker_id_card_filename(worker) -> str:
    """Return a safe filename based on the public Worker ID."""
    raw = str(worker["worker_id"] or "worker").strip()
    safe = "".join(ch for ch in raw if ch.isalnum() or ch in {"-", "_"})
    return f"Sivdhi-Worker-ID-{safe or 'worker'}.svg"
