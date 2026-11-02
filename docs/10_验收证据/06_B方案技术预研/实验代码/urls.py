from django.urls import path
from probe.views import login_view, ReadView, WriteView, ReceptionView, ExportView, AuditView
from probe.services import PERMISSIONS

urlpatterns = [path('probe/login',login_view),path('probe/encounters',ReceptionView.as_view()),
    path('probe/encounters/<uuid:encounter_id>',ReadView.as_view()),path('probe/exports',ExportView.as_view()),
    path('probe/audit',AuditView.as_view())]
urlpatterns += [path('probe/encounters/<uuid:encounter_id>/'+mode,WriteView.as_view(mode=mode,permission_key=permission)) for mode,permission in PERMISSIONS.items()]
