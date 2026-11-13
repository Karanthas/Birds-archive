from django.urls import path
from archive import views
from archive import recognition_views
from archive import batch_views
from archive import guide_views
from archive import upload_views

urlpatterns = [
    path('delivery/token/', upload_views.token, name='delivery-token'),
    path('delivery/files/', upload_views.files, name='delivery-files'),
    path('delivery/receipts/<uuid:request_id>/', upload_views.receipts, name='delivery-receipt'),
    path('bird-info/<uuid:species_id>/', guide_views.information, name='guide_information'),
    path('guide/', guide_views.index, name='guide_index'),
    path('guide/<uuid:species_id>/', guide_views.detail, name='guide_detail'),
    path('management/preview/', views.management_preview, name='management-preview'),
    path('management/<uuid:preview_id>/', views.management_display, name='management-display'),
    path('management/<uuid:preview_id>/revise/', views.management_revise, name='management-revise'),
    path('management/<uuid:preview_id>/submit/', views.management_submit, name='management-submit'),
    path('review/batch/preview/', batch_views.preview, name='batch-preview'),
    path('review/batch/<uuid:preview_id>/', batch_views.display, name='batch-display'),
    path('review/batch/<uuid:preview_id>/revise/', batch_views.revise, name='batch-revise'),
    path('review/batch/<uuid:preview_id>/submit/', batch_views.submit, name='batch-submit'),
    path('recognition/status/', recognition_views.status, name='recognition-status'),
    path('recognition/rerun/', recognition_views.rerun, name='recognition-rerun'),
    path('photos/<uuid:photo_id>/recognition/retry/', recognition_views.retry, name='recognition-retry'),
    path("", views.gallery, name="gallery"),
    path("review/", views.workbench, name="workbench"),
    path("gallery/page/", views.gallery, name="gallery-page"),
    path('species/',views.catalog,name='catalog'),
    path('tasks/',views.tasks,name='tasks'),
    path('tasks/status/',views.task_status,name='task-status'),
    path('tasks/<uuid:job_id>/retry/',views.task_retry,name='task-retry'),
    path('metadata/location/', views.location, name='location'),
    path("photos/<uuid:photo_id>/", views.detail, name="detail"),
    path("photos/<uuid:photo_id>/download/", views.download_original, name="download-original"),
    path('photos/<uuid:photo_id>/capture-time/',views.capture_time,name='capture-time'),
    path("media/<uuid:photo_id>/<str:variant>/", views.media, name="media"),
]
