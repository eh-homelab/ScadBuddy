using Xunit;

namespace ScadBuddy.Tests;

public class DeepLinkTests
{
	const string Code = "AAAAAAAAAAAAAAAAAAAAAA";
	static readonly string Claim = $"https://sb.example/api/v1/devices/pairings/{Code}/claim";

	static Uri? Target(string link) => DeepLink.TargetOf(new Uri(link));

	static string Link(string claim) => "scadbuddy://pair?url=" + Uri.EscapeDataString(claim);

	[Fact]
	public void APairingLinkGivesItsClaimAddress() =>
		Assert.Equal(Claim, Target(Link(Claim))!.ToString());

	[Theory]
	[InlineData("http://sb.example/api/v1/devices/pairings/AAAAAAAAAAAAAAAAAAAAAA/claim")]
	[InlineData("https://user:pass@sb.example/api/v1/devices/pairings/AAAAAAAAAAAAAAAAAAAAAA/claim")]
	[InlineData("https://sb.example/api/v1/devices/pairings/AAAAAAAAAAAAAAAAAAAAAA/claim?x=1")]
	[InlineData("https://sb.example/api/v1/devices/pairings/AAAAAAAAAAAAAAAAAAAAAA/claim#x")]
	[InlineData("https://sb.example/api/v1/devices/pairings/short/claim")]
	[InlineData("https://sb.example/api/v1/jobs/abc")]
	[InlineData("https://sb.example/api/v1/devices/pairings/AAAAAAAAAAAAAAAAAAAAAA/claim/../../x")]
	public void AClaimAddressOfAnotherShapeIsRefused(string claim) => Assert.Null(Target(Link(claim)));

	[Theory]
	// The per-scan link of #8157 is gone: the pairing link replaces it.
	[InlineData("scadbuddy://scan?kind=nfc&url=https%3A%2F%2Fsb.example%2Fr")]
	[InlineData("scadbuddy://pair")]
	[InlineData("scadbuddy://pair?url=not-a-url")]
	[InlineData("https://pair?url=https%3A%2F%2Fsb.example%2Fapi%2Fv1%2Fdevices%2Fpairings%2FAAAAAAAAAAAAAAAAAAAAAA%2Fclaim")]
	public void OtherLinksAreRefused(string link) => Assert.Null(Target(link));

	[Fact]
	public void NoLinkIsNoPairing() => Assert.Null(DeepLink.TargetOf(null));

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

	[Fact]
	public void TheQuestionNamesTheServerInPunycode()
	{
		var claim = Target(Link($"https://scädbuddy.example/api/v1/devices/pairings/{Code}/claim"))!;
		var question = DeepLink.PairQuestion(claim, "https://sb.example");
		Assert.Contains("https://xn--scdbuddy-1za.example", question);
		Assert.DoesNotContain("scädbuddy", question);
	}

	[Fact]
	public void TheQuestionNamesTheSetAddressOnlyWhenItDiffers()
	{
		var claim = Target(Link(Claim))!;
		Assert.DoesNotContain("They differ", DeepLink.PairQuestion(claim, "https://SB.example"));
		var other = DeepLink.PairQuestion(claim, "https://mine.example");
		Assert.Contains("They differ", other);
		Assert.Contains("https://mine.example", other);
	}
}
