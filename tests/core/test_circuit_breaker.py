import unittest, time
from akita_ares.core.circuit_breaker import CircuitBreaker, CircuitBreakerState, CircuitBreakerOpenException
from akita_ares.core.logger import setup_logging 
setup_logging(level='CRITICAL', console_output=False, log_file=None) 
def mock_operation(fail=False, fail_times=0, success_after=0):
    if not hasattr(mock_operation, 'call_count'): mock_operation.call_count = 0; mock_operation.call_count += 1
    if fail: raise ValueError("Failed intentionally")
    if fail_times > 0 and mock_operation.call_count <= fail_times: raise ValueError(f"Failed intentionally ({mock_operation.call_count}/{fail_times})")
    if success_after > 0 and mock_operation.call_count <= success_after: raise ValueError(f"Failed intentionally until call {success_after+1}")
    return "Success"
class TestCircuitBreaker(unittest.TestCase):
    def setUp(self): mock_operation.call_count = 0
    def test_initial_state_closed(self): cb = CircuitBreaker(3, 1); self.assertEqual(cb.state, CircuitBreakerState.CLOSED)
    def test_success_remains_closed(self): cb = CircuitBreaker(3, 1); [self.assertEqual(cb.execute(mock_operation), "Success") for _ in range(5)]; self.assertEqual(cb.state, CircuitBreakerState.CLOSED); self.assertEqual(cb.failure_count, 0)
    def test_transition_to_open(self): cb = CircuitBreaker(2, 10); self.assertRaises(ValueError, cb.execute, mock_operation, fail=True); self.assertEqual(cb.state, CircuitBreakerState.CLOSED); self.assertEqual(cb.failure_count, 1); self.assertRaises(ValueError, cb.execute, mock_operation, fail=True); self.assertEqual(cb.state, CircuitBreakerState.OPEN); self.assertEqual(cb.failure_count, 2)
    def test_open_state_rejects_calls(self): cb = CircuitBreaker(1, 10); self.assertRaises(ValueError, cb.execute, mock_operation, fail=True); self.assertEqual(cb.state, CircuitBreakerState.OPEN); self.assertRaises(CircuitBreakerOpenException, cb.execute, mock_operation)
    def test_transition_to_half_open(self): rt = 0.1; cb = CircuitBreaker(1, rt); self.assertRaises(ValueError, cb.execute, mock_operation, fail=True); self.assertEqual(cb.state, CircuitBreakerState.OPEN); time.sleep(rt * 1.1); self.assertTrue(cb.last_failure_time and (time.monotonic() - cb.last_failure_time) > cb.recovery_timeout_seconds); mock_operation.call_count = 0; result = cb.execute(mock_operation); self.assertEqual(result, "Success"); self.assertEqual(cb.state, CircuitBreakerState.CLOSED) 
    def test_half_open_success_closes_circuit(self): rt = 0.1; cb = CircuitBreaker(1, rt); self.assertRaises(ValueError, cb.execute, mock_operation, fail=True); time.sleep(rt * 1.1); mock_operation.call_count = 0; result = cb.execute(mock_operation); self.assertEqual(result, "Success"); self.assertEqual(cb.state, CircuitBreakerState.CLOSED); self.assertEqual(cb.failure_count, 0)
    def test_half_open_failure_reopens_circuit(self): rt = 0.1; cb = CircuitBreaker(1, rt); self.assertRaises(ValueError, cb.execute, mock_operation, fail=True); time.sleep(rt * 1.1); mock_operation.call_count = 0; self.assertRaises(ValueError, cb.execute, mock_operation, fail=True); self.assertEqual(cb.state, CircuitBreakerState.OPEN); self.assertEqual(cb.failure_count, 2)
    def test_reset_after_success_in_closed(self): cb = CircuitBreaker(3, 10); self.assertRaises(ValueError, cb.execute, mock_operation, fail=True); self.assertEqual(cb.failure_count, 1); result = cb.execute(mock_operation); self.assertEqual(result, "Success"); self.assertEqual(cb.failure_count, 0); self.assertRaises(ValueError, cb.execute, mock_operation, fail=True); self.assertRaises(ValueError, cb.execute, mock_operation, fail=True); self.assertEqual(cb.failure_count, 2); self.assertEqual(cb.state, CircuitBreakerState.CLOSED)
