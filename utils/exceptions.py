class ReplxException(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message

    def __str__(self):
        return f"{self.__class__.__name__}: {self.message}"


class TransportError(ReplxException):
    pass


class ProtocolError(ReplxException):
    pass


class FileSystemError(ReplxException):
    pass


class ValidationError(ReplxException):
    pass


class CompilationError(ReplxException):
    pass


class PkgError(ReplxException):
    pass
