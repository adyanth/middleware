import time

from middlewared.service import CallError


class Authenticator:

    NAME = NotImplementedError
    PROPAGATION_DELAY = NotImplementedError
    SCHEMA_MODEL = NotImplementedError

    def __init__(self, middleware, attributes):
        self.middleware = middleware
        self.attributes = attributes
        self.initialize_credentials()

    def initialize_credentials(self):
        pass

    @staticmethod
    async def validate_credentials(middleware, data):
        raise NotImplementedError

    def perform(self, domain, validation_name, validation_content):
        try:
            perform_ret = self._perform(domain, validation_name, validation_content)
        except Exception as e:
            raise CallError(f'Failed to perform {self.NAME} challenge for {domain!r} domain: {e}')
        else:
            self.wait_for_records_to_propagate(perform_ret)

    def _perform(self, domain, validation_name, validation_content):
        raise NotImplementedError

    def wait_for_records_to_propagate(self, perform_ret):
        time.sleep(self.PROPAGATION_DELAY)

    def cleanup(self, domain, validation_name, validation_content):
        try:
            self._cleanup(domain, validation_name, validation_content)
        except Exception as e:
            raise CallError(f'Failed to cleanup {self.NAME} challenge for {domain!r} domain: {e}')

    def _cleanup(self, domain, validation_name, validation_content):
        raise NotImplementedError
