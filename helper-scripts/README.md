Just a collection of helper scripts for the development of the module.

detect-prop-overlap.py: A script to detect properties in a file that are using the same name or property ID. Looks for instances of `_ensureSetX` to find conflicts and outputs a list of anything that is duplicated. Takes in the path to a file.

export-multi-test.py: Builds `example-msg-files/multi-stream.msg`, the fixture used by the multiple value stream tests.

produce-named-catalog-test.py: Builds `example-msg-files/named-property-catalog.msg`, the fixture for the named property catalog tests. It creates a parent message and an embedded forwarded message that share named property definitions whose textual identifiers collide (a numerical and a string property both named "000A", plus another string "000A" in a second property set). Run it from the repository root to (re)generate the fixture.
