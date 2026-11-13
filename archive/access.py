from django.utils.cache import patch_cache_control

from archive.services.maintenance import MaintenanceError, request_barrier


class _BarrierStreamingIterator:
    def __init__(self, iterator, barrier):
        self.iterator = iter(iterator)
        self.barrier = barrier
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return next(self.iterator)
        except StopIteration:
            self.close()
            raise

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            close = getattr(self.iterator, "close", None)
            if close:
                close()
        finally:
            self.barrier.__exit__(None, None, None)


class PrivateResponseMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.get_host()  # 校验入口 Host；个人应用无需身份会话。
        barrier = request_barrier()
        entered = False
        try:
            barrier.__enter__()
            entered = True
            response = self.get_response(request)
        except MaintenanceError:
            if entered:
                barrier.__exit__(*__import__("sys").exc_info())
            from django.http import JsonResponse
            response = JsonResponse({"status": "maintenance", "message": "系统正在维护，请稍后重试。"}, status=503)
        except BaseException:
            if entered:
                barrier.__exit__(*__import__("sys").exc_info())
            raise
        else:
            if response.streaming:
                response.streaming_content = _BarrierStreamingIterator(response.streaming_content, barrier)
            else:
                barrier.__exit__(None, None, None)
        patch_cache_control(response, private=True, no_store=True, no_cache=True, must_revalidate=True)
        response["Pragma"] = "no-cache"
        response["Expires"] = "0"
        return response
