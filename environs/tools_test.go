package environs_test

import (
	"fmt"
	"io/ioutil"
	. "launchpad.net/gocheck"
	"launchpad.net/juju-core/environs"
	"launchpad.net/juju-core/environs/config"
	"launchpad.net/juju-core/environs/dummy"
	"launchpad.net/juju-core/version"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

type ToolsSuite struct {
	env environs.Environ
}

func (t *ToolsSuite) SetUpTest(c *C) {
	env, err := environs.NewFromAttrs(map[string]interface{}{
		"name":            "test",
		"type":            "dummy",
		"zookeeper":       false,
		"authorized-keys": "i-am-a-key",
	})
	c.Assert(err, IsNil)
	t.env = env
}

func (t *ToolsSuite) TearDownTest(c *C) {
	dummy.Reset()
}

var envs *environs.Environs

var currentToolsPath = mkToolsPath(version.Current.String())

func mkVersion(vers string) version.Version {
	v, err := version.Parse(vers)
	if err != nil {
		panic(err)
	}
	return v
}

func mkToolsPath(vers string) string {
	return environs.ToolsetPath(version.BinaryVersion{
		Version: mkVersion(vers),
		Series:  config.CurrentSeries,
		Arch:    config.CurrentArch,
	})
}

var _ = Suite(&ToolsSuite{})

func (t *ToolsSuite) TestPutTools(c *C) {
	toolset, err := environs.PutToolset(t.env.Storage())
	c.Assert(err, IsNil)
	c.Assert(toolset.BinaryVersion, Equals, version.Current)
	c.Assert(toolset.URL, Not(Equals), "")

	for i, getTools := range []func(url, dir string) error{
		environs.GetToolset,
		getToolsWithTar,
	} {
		c.Logf("test %d", i)
		// Unarchive the tool executables into a temp directory.
		dir := c.MkDir()
		err = getTools(toolset.URL, dir)
		c.Assert(err, IsNil)

		// Verify that each tool executes and produces some
		// characteristic output.
		for _, tool := range []string{"juju", "jujud"} {
			out, err := exec.Command(filepath.Join(dir, tool), "arble").CombinedOutput()
			if err != nil {
				c.Assert(err, FitsTypeOf, (*exec.ExitError)(nil))
			}
			c.Check(string(out), Matches, fmt.Sprintf(`usage: %s (.|\n)*`, tool))
		}
	}
}

// getToolsWithTar is the same as GetTools but uses tar
// itself so we're not just testing the Go tar package against
// itself.
func getToolsWithTar(url, dir string) error {
	resp, err := http.Get(url)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	// unarchive using actual tar command so we're
	// not just verifying the Go tar package against itself.
	cmd := exec.Command("tar", "xz")
	cmd.Dir = dir
	cmd.Stdin = resp.Body
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("tar extract failed: %s", out)
	}
	return nil
}

// Test that the upload procedure fails correctly
// when the build process fails (because of a bad Go source
// file in this case).
func (t *ToolsSuite) TestUploadBadBuild(c *C) {
	gopath := c.MkDir()
	join := append([]string{gopath, "src"}, strings.Split("launchpad.net/juju-core/cmd/broken", "/")...)
	pkgdir := filepath.Join(join...)
	err := os.MkdirAll(pkgdir, 0777)
	c.Assert(err, IsNil)

	err = ioutil.WriteFile(filepath.Join(pkgdir, "broken.go"), []byte("nope"), 0666)
	c.Assert(err, IsNil)

	defer os.Setenv("GOPATH", os.Getenv("GOPATH"))
	os.Setenv("GOPATH", gopath)

	toolset, err := environs.PutToolset(t.env.Storage())
	c.Assert(toolset, IsNil)
	c.Assert(err, ErrorMatches, `build failed: exit status 1; can't load package:(.|\n)*`)
}

type toolsSpec struct {
	version string
	os      string
	arch    string
}

var findToolsTests = []struct {
	version        version.Version // version to assume is current for the test.
	contents       []string        // names in private storage.
	publicContents []string        // names in public storage.
	expect         string          // the name we expect to find (if no error).
	err            string          // the error we expect to find (if not blank).
}{{
	// current version should be satisfied by current tools path.
	version:  version.Current.Version,
	contents: []string{currentToolsPath},
	expect:   currentToolsPath,
}, {
	// major versions don't match.
	version: mkVersion("1.0.0"),
	contents: []string{
		mkToolsPath("0.0.9"),
	},
	err: "no compatible tools found",
}, {
	// major versions don't match.
	version: mkVersion("1.0.0"),
	contents: []string{
		mkToolsPath("2.0.9"),
	},
	err: "no compatible tools found",
}, {
	// fall back to public storage when nothing found in private.
	version: mkVersion("1.0.0"),
	contents: []string{
		mkToolsPath("0.0.9"),
	},
	publicContents: []string{
		mkToolsPath("1.0.0"),
	},
	expect: "public-" + mkToolsPath("1.0.0"),
}, {
	// always use private storage in preference to public storage.
	version: mkVersion("1.0.0"),
	contents: []string{
		mkToolsPath("1.0.2"),
	},
	publicContents: []string{
		mkToolsPath("1.0.9"),
	},
	expect: mkToolsPath("1.0.2"),
}, {
	// we'll use an earlier version if the major version number matches.
	version: mkVersion("1.99.99"),
	contents: []string{
		mkToolsPath("1.0.0"),
	},
	expect: mkToolsPath("1.0.0"),
}, {
	// check that version comparing is numeric, not alphabetical.
	version: mkVersion("1.0.0"),
	contents: []string{
		mkToolsPath("1.0.9"),
		mkToolsPath("1.0.10"),
		mkToolsPath("1.0.11"),
	},
	expect: mkToolsPath("1.0.11"),
}, {
	// minor version wins over patch version.
	version: mkVersion("1.0.0"),
	contents: []string{
		mkToolsPath("1.9.11"),
		mkToolsPath("1.10.10"),
		mkToolsPath("1.11.9"),
	},
	expect: mkToolsPath("1.11.9"),
}, {
	// mismatching series or architecture is ignored.
	version: mkVersion("1.0.0"),
	contents: []string{
		environs.ToolsetPath(version.BinaryVersion{
			Version: mkVersion("1.9.9"),
			Series:  "foo",
			Arch:    config.CurrentArch,
		}),
		environs.ToolsetPath(version.BinaryVersion{
			Version: mkVersion("1.9.9"),
			Series:  config.CurrentSeries,
			Arch:    "foo",
		}),
		mkToolsPath("1.0.0"),
	},
	expect: mkToolsPath("1.0.0"),
},
}

func (t *ToolsSuite) TestFindTools(c *C) {
	for i, tt := range findToolsTests {
		c.Logf("test %d", i)
		for _, name := range tt.contents {
			err := t.env.Storage().Put(name, strings.NewReader(name), int64(len(name)))
			c.Assert(err, IsNil)
		}
		// The contents of all files in the public storage is prefixed with "public-" so
		// that we can easily tell if we've got the right thing.
		for _, name := range tt.publicContents {
			data := "public-" + name
			err := t.env.PublicStorage().(environs.Storage).Put(name, strings.NewReader(data), int64(len(data)))
			c.Assert(err, IsNil)
		}
		vers := version.BinaryVersion{
			Version: tt.version,
			Series:  config.CurrentSeries,
			Arch:    config.CurrentArch,
		}
		toolset, err := environs.FindToolset(t.env, vers)
		if tt.err != "" {
			c.Assert(err, ErrorMatches, tt.err)
		} else {
			c.Assert(err, IsNil)
			resp, err := http.Get(toolset.URL)
			c.Assert(err, IsNil)
			data, err := ioutil.ReadAll(resp.Body)
			c.Assert(err, IsNil)
			c.Assert(string(data), Equals, tt.expect, Commentf("url %s", toolset.URL))
		}
		t.env.Destroy(nil)
	}
}

var setenvTests = []struct {
	set    string
	expect []string
}{
	{"foo=1", []string{"foo=1", "arble="}},
	{"foo=", []string{"foo=", "arble="}},
	{"arble=23", []string{"foo=bar", "arble=23"}},
	{"zaphod=42", []string{"foo=bar", "arble=", "zaphod=42"}},
}

func (*ToolsSuite) TestSetenv(c *C) {
	env0 := []string{"foo=bar", "arble="}
	for i, t := range setenvTests {
		c.Logf("test %d", i)
		env := make([]string, len(env0))
		copy(env, env0)
		env = environs.Setenv(env, t.set)
		c.Check(env, DeepEquals, t.expect)
	}
}
