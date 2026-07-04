import re

from django.conf import settings
from django.shortcuts import redirect, render

# Must stay in sync with the WebSocket route pattern in party/routing.py, or a
# room page could render whose socket can never connect.
ROOM_ID_RE = re.compile(r"^[\w-]{1,64}$")


def index(request):
    return render(request, "party/index.html")


def room(request, room_id):
    if not ROOM_ID_RE.match(room_id):
        return redirect("index")
    ice_servers = [{"urls": "stun:stun.l.google.com:19302"}]
    if settings.TURN_URL:
        ice_servers.append(
            {
                "urls": settings.TURN_URL,
                "username": settings.TURN_USERNAME,
                "credential": settings.TURN_CREDENTIAL,
            }
        )
    return render(
        request,
        "party/room.html",
        {"room_id": room_id, "ice_servers": ice_servers},
    )
