# Order Service

```python
from external_telemetry_sdk import TelemetryClient
from service import OrderService

TelemetryClient.flush_metrics()
svc = OrderService()
svc.process_order(10)
```

