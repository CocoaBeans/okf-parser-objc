"""Unit tests for okf-parser-objc (run: python -m unittest discover tests)."""
import tempfile
import unittest
from pathlib import Path

from okf_parser_objc import ObjCParser, scrub

SAMPLE = b'''#import <Foundation/Foundation.h>
#import "VIPThing.h"
@import GZIP;
@class VIPOther;
NS_ASSUME_NONNULL_BEGIN
/// A sample class.
@interface Foo : NSObject <Bar, Baz>
@property (nonatomic, copy, nullable) NSString *name;
@property (nonatomic, assign) NSInteger count;
- (void)doA:(int)a withB:(NSString * _Nullable)b NS_SWIFT_NAME(do(a:b:));
+ (instancetype _Nonnull)shared;
- (void)plain;
@end
NS_ASSUME_NONNULL_END

@protocol Bar <NSObject>
- (void)barMethod;
@end

@interface Foo ()
@property (nonatomic, strong) NSArray *privateThing;
@end

@implementation Foo
/// Does A with B.
- (void)doA:(int)a withB:(NSString *)b {
    [self plain];
    [[NSNotificationCenter defaultCenter] postNotificationName:@"x" object:self userInfo:nil];
    self.name = [Foo new];
    helper(1);
    dispatch_async(dispatch_get_main_queue(), ^{ [self doA:2 withB:b]; });
}
+ (instancetype)shared { return nil; }
- (void)plain { }
@end

@implementation Foo (Extra)
- (void)extra { [self plain]; }
@end

static int helper(int x) { return x; }
'''


def _parse(src: bytes, name="Foo.m"):
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        f = root / name
        f.write_bytes(src)
        return ObjCParser().parse_file(f, root)


class ScrubTests(unittest.TestCase):
    def test_scrub_preserves_length_and_newlines(self):
        out = scrub(SAMPLE)
        self.assertEqual(len(out), len(SAMPLE))
        self.assertEqual(out.count(b"\n"), SAMPLE.count(b"\n"))
        self.assertNotIn(b"NS_ASSUME_NONNULL_BEGIN", out)
        self.assertNotIn(b"_Nullable", out)


class ParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.concepts = _parse(SAMPLE)
        cls.by_title = {}
        for c in cls.concepts:
            cls.by_title.setdefault(c.title, []).append(c)

    def _one(self, title, ctype):
        found = [c for c in self.by_title.get(title, []) if c.type == ctype]
        self.assertEqual(len(found), 1, f"{ctype} {title!r}: {found}")
        return found[0]

    def test_module_imports(self):
        mod = self.concepts[0]
        self.assertEqual(mod.type, "Module")
        self.assertEqual(mod.imports, ["Foundation/Foundation.h", "VIPThing.h", "GZIP"])

    def test_interface_despite_nonnull_macros(self):
        classes = [c for c in self.by_title["Foo"] if c.type == "Class"]
        self.assertEqual(len(classes), 2)              # @interface + @implementation
        iface = next(c for c in classes if c.signature.startswith("@interface"))
        self.assertEqual(iface.signature, "@interface Foo : NSObject <Bar, Baz>")
        self.assertEqual(iface.inheritance, ["NSObject", "Bar", "Baz"])
        self.assertEqual(iface.methods, ["doA:withB:", "shared", "plain"])
        self.assertEqual(iface.description, "A sample class.")
        self.assertEqual([f["name"] for f in iface.fields], ["name", "count"])
        self.assertEqual(iface.fields[0]["type"], "NSString *")
        self.assertIn("copy", iface.fields[0]["visibility"])

    def test_class_extension_is_skipped(self):
        self.assertFalse(any(c.title == "Foo" and "()" in c.signature for c in self.concepts))

    def test_protocol(self):
        bar = self._one("Bar", "Interface")
        self.assertEqual(bar.inheritance, ["NSObject"])
        self.assertEqual(bar.methods, ["barMethod"])

    def test_implementation_methods(self):
        impl = next(c for c in self.by_title["Foo"] if c.signature == "@implementation Foo")
        self.assertEqual(impl.methods, ["doA:withB:", "shared", "plain"])
        m = self._one("doA:withB:", "Function")
        self.assertEqual(m.signature, "- (void)doA:(int)a withB:(NSString *)b")
        self.assertEqual(m.description, "Does A with B.")
        self.assertEqual(m.related, ["Foo/Foo"])
        self.assertEqual(m.source_lines, (26, 32))

    def test_calls_are_full_selectors(self):
        m = self._one("doA:withB:", "Function")
        for expected in ("plain", "postNotificationName:object:userInfo:", "defaultCenter",
                         "NSNotificationCenter", "new", "Foo", "helper", "dispatch_async",
                         "dispatch_get_main_queue", "doA:withB:"):
            self.assertIn(expected, m.calls_raw)

    def test_category(self):
        cat = self._one("Foo+Extra", "Class")
        self.assertEqual(cat.signature, "@implementation Foo (Extra)")
        extra = self._one("extra", "Function")
        self.assertEqual(extra.related, ["Foo/Foo_Extra"])

    def test_c_function(self):
        h = self._one("helper", "Function")
        self.assertEqual(h.signature, "int helper(int x)")

    def test_header_extension_claimed(self):
        self.assertEqual(ObjCParser.EXTENSIONS, {".m", ".mm", ".h"})
        cs = _parse(b"@interface Only : NSObject\n@end\n", "Only.h")
        self.assertEqual([c.type for c in cs], ["Module", "Class"])


if __name__ == "__main__":
    unittest.main()
