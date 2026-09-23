from django.conf import settings
from django.core.mail import send_mail
def alert(subject, message):
    if settings.ALERT_EMAIL:
        send_mail(subject,message,settings.DEFAULT_FROM_EMAIL,[settings.ALERT_EMAIL],fail_silently=True)
