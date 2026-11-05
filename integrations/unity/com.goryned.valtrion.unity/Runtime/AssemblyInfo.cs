using UnityEngine.Scripting;

// Nothing in a project references Valtrion.Unity: the harness starts itself from a [RuntimeInitializeOnLoadMethod] (Harness/ValidationHarness.cs).
// Without this attribute UnityLinker drops the whole assembly from an IL2CPP player. A link.xml inside a package is not read by the linker,
// so this attribute is what keeps the harness; the linker then keeps the bootstrap methods and everything they reach.
[assembly: AlwaysLinkAssembly]
