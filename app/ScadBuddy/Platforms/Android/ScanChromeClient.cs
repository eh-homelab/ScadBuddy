using Android.Webkit;
using Microsoft.Maui.Handlers;
using Microsoft.Maui.Platform;

namespace ScadBuddy;

/// <summary>
/// Gives a ScadBuddy page the camera (#8155: every scan keeps a photo) and the phone's location
/// (every scan records where it was made). Only the set ScadBuddy origin gets them, and only
/// video capture of the page's permissions; any other request is denied. The file chooser and the
/// rest stay MAUI's.
/// </summary>
sealed class ScanChromeClient(IWebViewHandler handler, IReadOnlySet<string> origins) : MauiWebChromeClient(handler)
{
	bool Allowed(string? origin) => origin is not null && origins.Contains(origin.TrimEnd('/'));

	public override void OnPermissionRequest(PermissionRequest? request)
	{
		if (request is null) return;
		var resources = request.GetResources() ?? [];
		if (!Allowed(request.Origin?.ToString()) || resources.Length == 0 || resources.Any(r => r != PermissionRequest.ResourceVideoCapture))
		{
			request.Deny();
			return;
		}
		MainThread.BeginInvokeOnMainThread(async () =>
		{
			var status = await Permissions.RequestAsync<Permissions.Camera>();
			if (status == PermissionStatus.Granted) request.Grant(resources);
			else request.Deny();
		});
	}

	public override void OnGeolocationPermissionsShowPrompt(string? origin, GeolocationPermissions.ICallback? callback)
	{
		if (callback is null) return;
		if (!Allowed(origin))
		{
			callback.Invoke(origin, false, false);
			return;
		}
		MainThread.BeginInvokeOnMainThread(async () =>
		{
			var status = await Permissions.RequestAsync<Permissions.LocationWhenInUse>();
			callback.Invoke(origin, status == PermissionStatus.Granted, false);
		});
	}
}
