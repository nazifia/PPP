from django.urls import include, path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register('companies', views.CompanyViewSet, basename='company')
router.register('users', views.UserViewSet, basename='user')
router.register('departments', views.DepartmentViewSet, basename='department')
router.register('units', views.UnitViewSet, basename='unit')
router.register(
    'dispensing-units', views.DispensingUnitViewSet, basename='dispensing-unit',
)
router.register('formulations', views.FormulationViewSet, basename='formulation')
router.register('products', views.ProductViewSet, basename='product')
router.register('requisitions', views.RequisitionViewSet, basename='requisition')
router.register('requisition-lines', views.RequisitionLineViewSet, basename='requisition-line')
router.register('deliveries', views.DeliveryViewSet, basename='delivery')
router.register('invoices', views.InvoiceViewSet, basename='invoice')
router.register('payments', views.PaymentViewSet, basename='payment')
router.register('credits', views.CreditNoteViewSet, basename='credit')
router.register('stock-movements', views.StockMovementViewSet, basename='stock-movement')
router.register('transfers', views.TransferViewSet, basename='transfer')
router.register('audit-logs', views.AuditLogViewSet, basename='audit-log')

urlpatterns = [
    path('health/', views.health, name='health'),
    path('auth/register/', views.RegisterHospitalView.as_view(), name='register'),
    path('auth/login/', views.LoginView.as_view(), name='login'),
    path('auth/logout/', views.LogoutView.as_view(), name='logout'),
    path('auth/me/', views.MeView.as_view(), name='me'),
    path('auth/change-password/', views.ChangePasswordView.as_view(), name='change-password'),
    path('organization/', views.OrganizationView.as_view(), name='organization'),
    path('organizations/', views.organizations, name='organizations'),
    path('dashboard/', views.dashboard, name='dashboard'),
    path('', include(router.urls)),
]
