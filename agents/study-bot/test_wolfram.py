# test_wolfram.py
from wolframclient.evaluation import WolframLanguageSession
from wolframclient.language import wlexpr

session = WolframLanguageSession()

print("Starting Wolfram session...")
session.start()
print("Session started.")

# Test 1 — symbolic algebra
result = session.evaluate(wlexpr('ToString[Factor[x^2 - 5x + 6], InputForm]'))
print(f"Factor[x^2 - 5x + 6] = {result}")

# Test 2 — calculus
result = session.evaluate(wlexpr('ToString[D[Sin[x]^2, x], InputForm]'))
print(f"D[Sin[x]^2, x] = {result}")

# Test 3 — matrix inverse
result = session.evaluate(wlexpr('ToString[Inverse[{{1,2},{3,4}}], InputForm]'))
print(f"Inverse = {result}")

# Test 4 — LaTeX output
result = session.evaluate(wlexpr('ToString[TeXForm[Integrate[x^2 Sin[x], x]]]'))
print(f"LaTeX: {result}")

# Test 5 — numerical result
result = session.evaluate(wlexpr('ToString[N[Pi, 10], InputForm]'))
print(f"Pi to 10 digits: {result}")

session.terminate()
print("Done.")
