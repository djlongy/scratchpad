# Code examples

A language name after the opening fence turns on syntax highlighting. `title` adds a
file name, `linenums` adds line numbers, `hl_lines` highlights lines.

```py title="add_numbers.py" linenums="1"
def add_two_numbers(num1, num2):
    return num1 + num2


result = add_two_numbers(5, 3)
print("The sum is:", result)
```

```js title="concat.js" linenums="1" hl_lines="2-4"
function concatenateStrings(str1, str2) {
  return str1 + str2;
}

const result = concatenateStrings("Hello, ", "World!");
console.log("The concatenated string is:", result);
```

Inline code can be highlighted too with `pymdownx.inlinehilite`: `#!py print("hi")`.

The language names are Pygments lexer aliases: <https://pygments.org/docs/lexers/>.
