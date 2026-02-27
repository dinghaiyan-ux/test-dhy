def add(a, b):
    """Adds two numbers."""
    return a + b

def subtract(a, b):
    """Subtracts b from a."""
    return a - b

def multiply(a, b):
    """Multiplies two numbers."""
    return a * b

def divide(a, b):
    """Divides a by b."""
    if b == 0:
        return "Error: Division by zero"
    return a / b

def power(a, b):
    """Raises a to the power of b."""
    return a ** b

def greet(name):
    """Greets the user."""
    return f"Hello, {name}!"

def is_even(n):
    """Checks if a number is even."""
    return n % 2 == 0

def is_odd(n):
    """Checks if a number is odd."""
    return n % 2 != 0

def get_square(n):
    """Returns the square of a number."""
    return n * n

def get_cube(n):
    """Returns the cube of a number."""
    return n * n * n

# Line 45 is below
def test_function_for_comment():
    """This is a test function for inline comments."""
    print("Line 45: Ready for review!")
    print("Line 46: Another line.")
    print("Line 47: More code...")
    print("Line 48: Testing...")
    print("Line 49: Almost there.")
    print("Line 50: Done.")
