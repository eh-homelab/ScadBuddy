using System.Text.RegularExpressions;

namespace ScadBuddy;

// The pure part of DeepLink: which links are pairing links, the origin a pairing goes to, and
// the question the app asks. It uses no MAUI type, so ScadBuddy.Tests compiles this file on its
// own (net10.0).
public static partial class DeepLink
{
	public const string Scheme = "scadbuddy";
	public const string Host = "pair";

	// Core's claim route (tools/go/core/internal/devices, Store.Public): the code is 16 random
	// bytes, base64url without padding.
	static readonly Regex ClaimPath = new("^/api/v1/devices/pairings/[A-Za-z0-9_-]{22}/claim$", RegexOptions.CultureInvariant);

	/// <summary>
	/// The claim address a link gives, or null when it is not a ScadBuddy pairing link:
	/// <c>scadbuddy://pair?url=https://&lt;host&gt;/api/v1/devices/pairings/&lt;code&gt;/claim</c>,
	/// with no user, query or fragment in the address.
	/// </summary>
	public static Uri? TargetOf(Uri? link)
	{
		if (link is null || link.Scheme != Scheme || link.Host != Host) return null;
		var query = System.Web.HttpUtility.ParseQueryString(link.Query);
		if (!Uri.TryCreate(query["url"], UriKind.Absolute, out var claim)) return null;
		if (claim.Scheme != "https" || claim.UserInfo.Length != 0 || claim.Query.Length != 0 || claim.Fragment.Length != 0) return null;
		return ClaimPath.IsMatch(claim.AbsolutePath) ? claim : null;
	}

	/// <summary>The origin with its host in ASCII (punycode), so a look-alike host never reads as another.</summary>
	public static string OriginOf(Uri address) => $"{address.Scheme}://{address.IdnHost}{(address.IsDefaultPort ? "" : $":{address.Port}")}";

	/// <summary>
	/// The question the app asks before it pairs: the claim's origin in punycode, and the app's
	/// own set ScadBuddy origin too when the two differ.
	/// </summary>
	public static string PairQuestion(Uri claim, string ownOrigin)
	{
		var origin = OriginOf(claim);
		var question = $"Pair this phone with\n{origin}?\n\nThat server can then read and write NFC tags and take photos with this phone while the app is open.";
		if (!string.Equals(origin, ownOrigin, StringComparison.OrdinalIgnoreCase))
			question += $"\n\nThis app's ScadBuddy is\n{ownOrigin}\n\nThey differ. Pair only with a ScadBuddy you run.";
		return question;
	}
}
