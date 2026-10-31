# okf-parser-objc

Objective-C parser plugin for [okf-generator](https://pypi.org/project/okf-generator/).
okf ships 17 tree-sitter grammars but none for Objective-C, so `.m` / `.mm` files are
skipped and ObjC headers parse under the C grammar as empty modules. This plugin claims
`.m`, `.mm` and `.h` and uses `tree-sitter-objc`.

What it extracts:

| Source construct | OKF concept |
|---|---|
| `@interface Foo : Base <P1, P2>` | Class (`inheritance` = Base, P1, P2; `fields` = properties; `methods` = selectors) |
| `@interface Foo (Cat)` / `@implementation Foo (Cat)` | Class titled `Foo+Cat` |
| `@protocol P <Q>` | Interface |
| `@implementation Foo` | Class, plus one Function per method definition |
| `- (T)sel:(A)a with:(B)b { … }` | Function titled `sel:with:` with signature and resolved calls |
| C `function_definition` | Function |
| `#import` / `@import` | module imports |
| `[receiver sel:x with:y]` inside a body | raw call `sel:with:` |

Header-only macros that the grammar cannot parse (`NS_ASSUME_NONNULL_BEGIN`,
`NS_SWIFT_NAME(...)`, nullability qualifiers, availability attributes) are blanked to
spaces before parsing, so byte offsets and line numbers stay intact.

## Install into okf's pipx venv

```bash
pipx inject okf-generator tree-sitter-objc
pipx inject okf-generator -e ~/Development/github/okf-parser-objc
okf plugin list      # should now show: objc [.h, .m, .mm]
```

## Test

```bash
~/.local/pipx/venvs/okf-generator/bin/python -m pytest tests
```
