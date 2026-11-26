namespace ScadBuddy;

/// <summary>
/// ScadBuddy's own /scan page (#7905, spec 2026-11-26: the app's screens are the web page). The
/// page is loaded from the ScadBuddy URL the person sets once; links to another origin open in
/// the browser, so the NFC bridge is never offered to another site.
/// </summary>
public partial class MainPage : ContentPage
{
	const string UrlKey = "scadbuddy.url";
	const string DefaultUrl = "https://scadbuddy.internal.nullreference.io/scan";

	string url = Preferences.Get(UrlKey, DefaultUrl);

	public MainPage()
	{
		InitializeComponent();
		Web.Navigating += OnNavigating;
		Web.HandlerChanged += (_, _) =>
		{
			if (Web.Handler is null) return;
#if ANDROID
			try
			{
				NfcBridge.Attach((Android.Webkit.WebView)Web.Handler.PlatformView!, OriginOf(url));
			}
			catch (Exception error)
			{
				Dispatcher.Dispatch(() => DisplayAlertAsync("NFC is not available", error.Message, "OK"));
			}
#endif
			Web.Source = url;
		};
	}

	static string OriginOf(string address)
	{
		var uri = new Uri(address);
		return uri.GetLeftPart(UriPartial.Authority);
	}

	void OnNavigating(object? sender, WebNavigatingEventArgs e)
	{
		if (!Uri.TryCreate(e.Url, UriKind.Absolute, out var target)) return;
		if (target.Scheme is not ("http" or "https")) return;
		if (string.Equals(target.GetLeftPart(UriPartial.Authority), OriginOf(url), StringComparison.OrdinalIgnoreCase)) return;
		e.Cancel = true;
		_ = Launcher.OpenAsync(target);
	}

	async void OnServerClicked(object? sender, EventArgs e)
	{
		var entered = await DisplayPromptAsync(
			"ScadBuddy address",
			"The page this app opens. NFC works only on this address's site. The app restarts its page after a change.",
			initialValue: url,
			keyboard: Keyboard.Url);
		if (string.IsNullOrWhiteSpace(entered)) return;
		if (!Uri.TryCreate(entered.Trim(), UriKind.Absolute, out var parsed) || parsed.Scheme != "https")
		{
			await DisplayAlertAsync("Not changed", "Give a full https:// address.", "OK");
			return;
		}
		Preferences.Set(UrlKey, parsed.ToString());
		await DisplayAlertAsync("Saved", "Close the app and open it again to use the new address.", "OK");
	}

	void OnReloadClicked(object? sender, EventArgs e) => Web.Reload();

	protected override bool OnBackButtonPressed()
	{
		if (Web.CanGoBack)
		{
			Web.GoBack();
			return true;
		}
		return base.OnBackButtonPressed();
	}
}
