from django.urls import path

from party import views

urlpatterns = [
    path("", views.index, name="index"),
    path("room/<str:room_id>/", views.room, name="room"),
]
