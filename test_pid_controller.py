import unittest
from combined_mantasim2 import PIDController

class TestPIDController(unittest.TestCase):

    def setUp(self):
        # standard P=1.0, I=0.5, D=0.1
        self.pid = PIDController(kp=1.0, ki=0.5, kd=0.1)

    def test_dt_less_or_equal_zero(self):
        """Test that compute returns 0.0 when dt <= 0"""
        self.assertEqual(self.pid.compute(setpoint=10, measured_value=5, dt=0), 0.0)
        self.assertEqual(self.pid.compute(setpoint=10, measured_value=5, dt=-1.0), 0.0)

    def test_proportional_logic(self):
        """Test purely proportional logic (I=0, D=0)"""
        pid = PIDController(kp=2.0, ki=0.0, kd=0.0)
        # error = 10 - 5 = 5
        # output = 2.0 * 5 = 10.0
        result = pid.compute(setpoint=10, measured_value=5, dt=1.0)
        self.assertEqual(result, 10.0)

    def test_integral_logic(self):
        """Test integral logic over multiple ticks (P=0, D=0)"""
        pid = PIDController(kp=0.0, ki=1.0, kd=0.0)

        # Tick 1: error = 5, dt=1.0 -> integral = 5.0 -> output = 1.0 * 5.0 = 5.0
        result1 = pid.compute(setpoint=10, measured_value=5, dt=1.0)
        self.assertEqual(result1, 5.0)

        # Tick 2: error = 5, dt=1.0 -> integral = 5.0 + 5.0 = 10.0 -> output = 1.0 * 10.0 = 10.0
        result2 = pid.compute(setpoint=10, measured_value=5, dt=1.0)
        self.assertEqual(result2, 10.0)

    def test_derivative_logic(self):
        """Test derivative logic (P=0, I=0)"""
        pid = PIDController(kp=0.0, ki=0.0, kd=1.0)

        # Tick 1: error = 5, prev_error = 0, dt=1.0 -> derivative = (5 - 0)/1 = 5.0
        # output = 1.0 * 5.0 = 5.0
        result1 = pid.compute(setpoint=10, measured_value=5, dt=1.0)
        self.assertEqual(result1, 5.0)

        # Tick 2: error = 2, prev_error = 5, dt=1.0 -> derivative = (2 - 5)/1 = -3.0
        # output = 1.0 * -3.0 = -3.0
        result2 = pid.compute(setpoint=10, measured_value=8, dt=1.0)
        self.assertEqual(result2, -3.0)

    def test_combined_logic(self):
        """Test combined P, I, D logic"""
        pid = PIDController(kp=1.0, ki=0.5, kd=0.1)

        # Tick 1: error = 10, prev_error = 0, dt=2.0
        # P = 1.0 * 10 = 10.0
        # I = error * dt = 10 * 2.0 = 20.0; I_term = 0.5 * 20.0 = 10.0
        # D = (10 - 0)/2.0 = 5.0; D_term = 0.1 * 5.0 = 0.5
        # Total = 10.0 + 10.0 + 0.5 = 20.5
        result1 = pid.compute(setpoint=10, measured_value=0, dt=2.0)
        self.assertAlmostEqual(result1, 20.5)

        # Tick 2: error = 4, prev_error = 10, dt=1.0
        # P = 1.0 * 4 = 4.0
        # I = previous_integral + (error * dt) = 20.0 + 4.0 = 24.0; I_term = 0.5 * 24.0 = 12.0
        # D = (4 - 10)/1.0 = -6.0; D_term = 0.1 * -6.0 = -0.6
        # Total = 4.0 + 12.0 - 0.6 = 15.4
        result2 = pid.compute(setpoint=10, measured_value=6, dt=1.0)
        self.assertAlmostEqual(result2, 15.4)

if __name__ == '__main__':
    unittest.main()
