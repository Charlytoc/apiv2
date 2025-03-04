class C:
    """
    Shortener for a call, it'll be used to cover an api exception that returns multiple errors.
    """

    args: tuple
    kwargs: dict

    def __init__(self, *args: tuple, **kwargs: dict):
        self.args = args
        self.kwargs = kwargs
