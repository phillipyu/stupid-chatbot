import datetime
from zoneinfo import ZoneInfo

get_date_schema = {
    "name": "get_date",
    "type": "function",
    "description": "Given timezone, returns date and time in that timezone",
    "strict": True,
    "parameters": {
        "type": "object",
        "properties": {
            "timezone": {
                "type": "string",
                "description": "The IANA timezone string, e.g., 'America/New_York'.",
            },
        },
        "required": ["timezone"],
        "additionalProperties": False,
    },
}


def get_date(timezone: str) -> str:
    """
    Given timezone string, returns iso-formatted date and time in that timezone

    Note that both inputs and outputs are strings, because they need to be serializable for OpenAI's function calls
    """
    # Convert timezone string to timezone object (IANA names only)
    try:
        tz = ZoneInfo(timezone)
    except Exception:
        raise ValueError(f"Invalid IANA timezone string: {timezone}")

    # Given timezone, returns date and time in that timezone
    return datetime.datetime.now(tz).isoformat()
