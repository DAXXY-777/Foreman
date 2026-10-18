"""Names for the crew and the general playbook. Names are used in the prompts and on the live screen."""
import re

WORKER_NAMES = ["Mina", "Jo", "Kai", "Zed", "Ola", "Nia", "Ravi", "Tess"]
FOREMAN_NAME, REVIEWER_NAME, HISTORIAN_NAME = "Rex", "Vera", "Lore"


def worker_name(agent_id):
    """w1 -> Mina, w2 -> Jo, w3 -> Kai ..."""
    m = re.search(r"(\d+)$", str(agent_id))
    i = (int(m.group(1)) - 1) if m else 0
    return WORKER_NAMES[i % len(WORKER_NAMES)]


# General debugging know-how (not specific to any one bug). Stored in the graph as procedural memories, and shown to a
# worker as hints when the words of the ticket match.
PLAYBOOK = [
    {"keywords": ["keyerror", "missing", "unknown", "does not exist", "not exist", "absent", "not found", "not in the"],
     "summary": "Look for code that indexes a dict or table by an id without checking the id exists. Compare with a "
                "sibling function that already skips or reports missing ids and use the same pattern."},
    {"keywords": ["ignore", "ignored", "flag", "option", "parameter", "argument", "no effect", "not respected", "regex"],
     "summary": "Follow the argument from the public method down to the inner call. Find the line where it is dropped, "
                "replaced by a default, or not passed on."},
    {"keywords": ["off by one", "threshold", "boundary", "limit", "too late", "too early", "exactly", "at least", "more than", "fewer"],
     "summary": "Check comparison operators (> versus >=, < versus <=), range ends and length checks at the exact "
                "boundary the ticket mentions."},
    {"keywords": ["stale", "cache", "cached", "not refreshed", "flush", "old value", "previous", "after drop", "after clear", "restart", "reset"],
     "summary": "When a value is wrong after some operation, find the operation that should refresh or reset it, and "
                "check that it runs, runs in the right order, and covers every path (including the less common ones)."},
    {"keywords": ["directory", "folder", "path", "create", "creates", "mkdir", "file not found", "no such file"],
     "summary": "Find where the file or resource is opened or created and check that everything it needs (such as its "
                "parent folder) exists or is created first."},
]
