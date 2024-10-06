# Custom exception classes
class PackageNotFoundError(Exception):
    pass


class CacheError(Exception):
    pass


class NetworkError(Exception):
    pass


class MetadataError(Exception):
    pass
