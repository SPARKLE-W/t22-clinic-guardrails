from django.urls import path
from governance import views
from governance.services import GENERATE, REVIEW, ACTIVATE
from clinic import views as clinic
from clinic import reports
from clinic import collaboration
from clinic import exports

urlpatterns = [
    path('api/exports/clinical-records', exports.clinical_records),
    path('api/exports/alert-hits', exports.alert_hits),
    path('api/exports/alert-summary', exports.alert_summary),
    path('api/management/work-divisions', collaboration.registry),
    path('api/management/work-divisions/evidence', collaboration.available_evidence),
    path('api/management/work-divisions/reviews', collaboration.review_registry),
    path('api/reports/store-summary', reports.store_summary),
    path('api/clinical/encounters/<uuid:encounter_id>/suggestions', clinic.suggestion_list),
    path('api/clinical/suggestions/<uuid:suggestion_id>/adoption', clinic.suggestion_decision('adopt')),
    path('api/clinical/suggestions/<uuid:suggestion_id>/decline', clinic.suggestion_decision('decline')),
    path('api/clinical/encounters/<uuid:encounter_id>/medications', clinic.medication('append')),
    path('api/clinical/encounters/<uuid:encounter_id>/medications/corrections', clinic.medication('correct')),
    path('', views.home), path('app', views.console), path('app/', views.console),
    path('app/assets/<path:path>', views.frontend_asset), path('api/session', views.session_info),
    path('api/login', views.signin), path('api/logout', views.signout),
    path('api/candidates', views.candidates), path('api/candidates/<uuid:candidate_id>', views.candidate_detail),
    path('api/example-definition', views.example), path('api/candidates/generate', views.mutation_view(GENERATE, 'generate')),
    path('api/candidates/submit', views.mutation_view(GENERATE, 'submit')), path('api/candidates/review', views.mutation_view(REVIEW)),
    path('api/candidates/activate', views.mutation_view(ACTIVATE)), path('api/audit', views.audit),
    path('api/encounters', clinic.create), path('api/receptions', clinic.receptions),
    path('api/receptions/<uuid:encounter_id>', clinic.reception),
    path('api/encounters/<uuid:encounter_id>/corrections', clinic.change('correct')),
    path('api/encounters/<uuid:encounter_id>/misregistration', clinic.change('mark')),
    path('api/encounters/create-options/doctors', clinic.doctors), path('api/encounters/create-options/pet-reference-check', clinic.pet_check),
    path('api/clinical/encounters', clinic.clinicals), path('api/clinical/encounters/<uuid:encounter_id>', clinic.clinical),
    path('api/clinical/encounters/<uuid:encounter_id>/diagnosis', clinic.diagnosis('record')),
    path('api/clinical/encounters/<uuid:encounter_id>/diagnosis/corrections', clinic.diagnosis('correct')),
    path('api/clinical/alerts', clinic.alerts), path('api/clinical/alerts/<uuid:alert_id>/resolution', clinic.resolve),
    path('api/clinical/alerts/<uuid:alert_id>/encounter', clinic.alert_encounter),
]
