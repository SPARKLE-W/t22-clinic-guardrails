from django.db import connection
from django.test.utils import CaptureQueriesContext
from governance.models import Activation, Review
from governance.services import DomainError
from .tests import ClinicFixture
from .services import _template


class TemplateValidationTests(ClinicFixture):
    def test_approval_and_activation_are_verified_with_one_query(self):
        with CaptureQueriesContext(connection) as captured:
            result = _template(self.template, self.a.pk)

        self.assertEqual(self.template.envelope['payload']['demand']['templates'], result)
        approval_queries = [query['sql'] for query in captured
                            if 'governance_activation' in query['sql'].lower()]
        self.assertEqual(1, len(approval_queries))
        self.assertIn('governance_review', approval_queries[0].lower())

    def test_rejects_a_candidate_without_a_matching_approval(self):
        Review.objects.filter(candidate=self.template).update(decision='reject')

        with self.assertRaises(DomainError) as caught:
            _template(self.template, self.a.pk)

        self.assertEqual('TEMPLATE_UNAVAILABLE', caught.exception.code)

    def test_rejects_a_candidate_without_a_matching_store_activation(self):
        Activation.objects.filter(candidate=self.template).update(store=self.b)

        with self.assertRaises(DomainError) as caught:
            _template(self.template, self.a.pk)

        self.assertEqual('TEMPLATE_UNAVAILABLE', caught.exception.code)
