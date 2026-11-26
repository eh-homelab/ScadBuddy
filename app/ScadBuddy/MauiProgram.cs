using Microsoft.Extensions.Logging;
using Microsoft.Maui.LifecycleEvents;

namespace ScadBuddy;

public static class MauiProgram
{
	public static MauiApp CreateMauiApp()
	{
		var builder = MauiApp.CreateBuilder();
		builder
			.UseMauiApp<App>()
			.ConfigureFonts(fonts =>
			{
				fonts.AddFont("OpenSans-Regular.ttf", "OpenSansRegular");
				fonts.AddFont("OpenSans-Semibold.ttf", "OpenSansSemibold");
			})
			.ConfigureLifecycleEvents(events =>
			{
#if ANDROID
				events.AddAndroid(android => android
					.OnResume(activity => NfcBridge.Instance.Resume(activity))
					.OnPause(activity => NfcBridge.Instance.Pause(activity)));
#endif
			});

#if ANDROID
		Microsoft.Maui.Handlers.WebViewHandler.Mapper.AppendToMapping("ScadBuddySettings", (handler, _) =>
		{
			var settings = handler.PlatformView.Settings;
			settings.JavaScriptEnabled = true;
			settings.DomStorageEnabled = true;
			Android.Webkit.CookieManager.Instance?.SetAcceptCookie(true);
		});
#endif

#if DEBUG
		builder.Logging.AddDebug();
#endif

		return builder.Build();
	}
}
