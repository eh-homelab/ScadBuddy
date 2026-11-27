using Xunit;

namespace ScadBuddy.Tests;

public class DeepLinkTests
{
	static ScanJob? Target(string link) => DeepLink.TargetOf(new Uri(link));

	[Fact]
	public void AScanLinkGivesItsKindAndResult()
	{
		var job = Target("scadbuddy://scan?kind=code&url=https%3A%2F%2Fsb.example%2Fapi%2Fv1%2Fjobs%2Fabc");
		Assert.NotNull(job);
		Assert.Equal("code", job.Kind);
		Assert.Equal("https://sb.example/api/v1/jobs/abc", job.Result.ToString());
	}

	[Fact]
	public void TheKindIsNfcWhenTheLinkNamesNone() =>
		Assert.Equal("nfc", Target("scadbuddy://scan?url=https%3A%2F%2Fsb.example%2Fr")!.Kind);

	[Theory]
	[InlineData("scadbuddy://scan?kind=nfc&url=http%3A%2F%2Fsb.example%2Fr")]
	[InlineData("scadbuddy://scan?kind=nfc&url=https%3A%2F%2Fuser%3Apass%40sb.example%2Fr")]
	[InlineData("scadbuddy://scan?kind=camera&url=https%3A%2F%2Fsb.example%2Fr")]
	[InlineData("scadbuddy://scan?kind=nfc")]
	[InlineData("scadbuddy://scan?kind=nfc&url=not-a-url")]
	[InlineData("scadbuddy://pair?url=https%3A%2F%2Fsb.example%2Fr")]
	[InlineData("https://scan?kind=nfc&url=https%3A%2F%2Fsb.example%2Fr")]
	public void OtherLinksAreRefused(string link) => Assert.Null(Target(link));

	[Fact]
	public void NoLinkIsNoScan() => Assert.Null(DeepLink.TargetOf(null));

	[Fact]
	public void AnInternationalHostIsComparedInPunycode()
	{
		Assert.Equal("https://xn--scdbuddy-1za.example", DeepLink.OriginOf(new Uri("https://scädbuddy.example/scan")));
		Assert.NotEqual(DeepLink.OriginOf(new Uri("https://scadbuddy.example/")), DeepLink.OriginOf(new Uri("https://scädbuddy.example/")));
	}

	[Fact]
	public void TheOriginKeepsAPortThatIsNotTheDefault()
	{
		Assert.Equal("https://sb.example:8443", DeepLink.OriginOf(new Uri("https://sb.example:8443/scan")));
		Assert.Equal("https://sb.example", DeepLink.OriginOf(new Uri("https://sb.example:443/scan")));
	}
}
