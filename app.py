#!/usr/bin/env python3
import os
import sys
# Tell Python to search the current project root folder for modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import aws_cdk as cdk
from cdk_stack.salon_booking_stack import SalonBookingStack

app = cdk.App()
SalonBookingStack(app, "SalonBookingStack")
app.synth()
