"""Run existing source tests with an explicit synthetic service compatibility shim."""
import argparse
from pathlib import Path
import sys
import unittest

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / 'src')]
from reception_service import service as service_module
from reception_service import integrations
from reception_test_support.demo import DemoIntegration
from reception_test_support.service import SyntheticReceptionService, _factory, _initialize_fixture


class CompatibleService(SyntheticReceptionService):
    def __init__(self, data_root, mode='demo', seller_id='', emit=None, **kwargs):
        if mode == 'demo':
            kwargs.update(_integration_factory=service_module.build_integrations, _fixture_initializer=_initialize_fixture)
        super().__init__(data_root, mode, seller_id, emit, **kwargs)


original_factory = integrations.build_integrations

def compatible_factory(mode, data_dir, settings):
    if mode == 'demo':
        return DemoIntegration(data_dir, settings)
    return original_factory(mode, data_dir, settings)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('pattern')
    parser.add_argument('-k', dest='test_name')
    args = parser.parse_args()
    service_module.ReceptionService = CompatibleService
    integrations.build_integrations = compatible_factory
    service_module.build_integrations = compatible_factory
    if args.test_name:
        unittest.defaultTestLoader.testNamePatterns = ['*' + args.test_name + '*']
    suite = unittest.defaultTestLoader.discover(str(HERE.parent / 'tests'), pattern=args.pattern)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    sys.exit(main())
