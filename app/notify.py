import httpx
import logging
from typing import Optional

logger = logging.getLogger(__name__)

async def notify_webhook(webhook_url: str, payload: dict) -> None:
    """
    Sends a generic JSON payload to the specified webhook URL.
    This can be a Discord or Slack incoming webhook.
    """
    if not webhook_url:
        return

    # If it's a Discord webhook, we should format it nicely
    if "discord.com/api/webhooks" in webhook_url:
        data = {
            "embeds": [{
                "title": f"🚨 PelosiTracker Signal: {payload.get('bot_name', 'Bot')} Executed!",
                "color": 2746326, # Cyan
                "fields": [
                    {"name": "Event", "value": payload.get('event_name', 'Unknown'), "inline": False},
                    {"name": "Selection", "value": f"{payload.get('market')} / {payload.get('outcome')}", "inline": True},
                    {"name": "Action", "value": payload.get('action', 'PLACE_BET'), "inline": True},
                    {"name": "Stake", "value": f"${payload.get('stake', 0.0):.2f}", "inline": True},
                    {"name": "Target Entry", "value": f"{payload.get('entry_price', 0.0)*100:.1f}¢", "inline": True},
                    {"name": "Estimated Edge", "value": f"{payload.get('edge', 0.0)*100:.1f}%", "inline": True},
                ],
                "footer": {"text": "Pelosi Alpha Paper Trade"}
            }]
        }
    else:
        # Fallback to generic JSON
        data = payload

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(webhook_url, json=data, timeout=10.0)
            if response.status_code >= 400:
                logger.warning(f"Webhook failed with status {response.status_code}: {response.text}")
    except Exception as e:
        logger.error(f"Failed to send webhook: {e}")
