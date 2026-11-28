namespace ScadBuddy;

/// <summary>
/// "Pair this phone" (#8244). A signed-in ScadBuddy page makes a one-time pairing code and shows
/// <c>scadbuddy://pair?url=&lt;claim address&gt;</c> as a QR code and a tap link. The app asks the
/// person, then claims the code and keeps the device token it gets (<see cref="DeviceLink"/>).
/// Any page can make such a link, so the app always asks first, and names the claim's origin in
/// punycode (<c>MainPage.Pair</c>).
/// </summary>
public static partial class DeepLink
{
	static Uri? pending;
	static string? pendingSpool;

	/// <summary>Raised on the main thread with the claim address of a link that came while the app was open.</summary>
	public static event Action<Uri>? Opened;

	/// <summary>Raised on the main thread with the spool id of a spool label's link (#8550).</summary>
	public static event Action<string>? SpoolOpened;

	public static void Raise(Uri? link)
	{
		if (SpoolOf(link) is { } spool)
		{
			if (SpoolOpened is { } spoolOpened) MainThread.BeginInvokeOnMainThread(() => spoolOpened(spool));
			else pendingSpool = spool;
			return;
		}
		var target = TargetOf(link);
		if (target is null) return;
		if (Opened is { } opened) MainThread.BeginInvokeOnMainThread(() => opened(target));
		else pending = target;
	}

	/// <summary>The spool label the app was started with, once.</summary>
	public static string? TakeSpool()
	{
		var spool = pendingSpool;
		pendingSpool = null;
		return spool;
	}

	/// <summary>The link the app was started with, once.</summary>
	public static Uri? Take()
	{
		var target = pending;
		pending = null;
		return target;
	}
}
