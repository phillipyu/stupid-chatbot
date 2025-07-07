import ast
import datetime
import subprocess
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

run_python_code_schema = {
    "name": "run_python_code",
    "type": "function",
    "description": "Runs short, one-line SINGLE-EXPRESSION Python code in its own subprocess.",
    "strict": True,
    "parameters": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "The one-line, single-expression code to run as a string.",
            },
        },
        "required": ["code"],
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


def run_python_code(code: str) -> str:
    """
    Runs arbitrary Python code in its own subprocess.

    Note: this is NOT secure right now! To productionize it, we'd probably want to isolate it to its own Docker container.
    """
    try:
        ast.parse(code, mode="eval")  # succeeds only for one expression
        code = f"print({code})"
    except SyntaxError:
        # Return an error message that the LLM can use to inform its next prompt
        return "Error: Only single-line expressions are allowed..."

    try:
        proc = subprocess.run(
            ["python", "-"],  # “-” → read code from stdin
            input=code.encode(),
            capture_output=True,  # pipe stdout & stderr
            timeout=2,  # kill after 2 seconds
        )
        answer = proc.stdout.decode()
        print("answer: " + answer)
        return answer
    except Exception as e:
        return f"Error: {str(e)}"
