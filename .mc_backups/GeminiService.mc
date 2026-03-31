import Toybox.Communications;
import Toybox.Lang;
import Toybox.WatchUi;

class GeminiService {
    // Replace with your proxy URL when ready
    private var _url = "https://your-api-gateway.com/gemini"; 
    private var _view as GarminBenfishView;

    function initialize(view as GarminBenfishView) {
        _view = view;
    }

    function makeRequest() as Void {
        var options = {
            :method => Communications.HTTP_METHOD_POST,
            :headers => {
                "Content-Type" => Communications.REQUEST_CONTENT_TYPE_JSON
            },
            :responseType => Communications.HTTP_RESPONSE_CONTENT_TYPE_JSON
        };

        var params = {
            "prompt" => "Hello Gemini, I am Benfish. Give me a short, cool sci-fi fact."
        };

        Communications.makeWebRequest(_url, params, options, method(:onReceive));
    }

    function onReceive(responseCode as Number, data as Dictionary?) as Void {
        if (responseCode == 200 && data != null) {
            var text = data.get("response");
            if (text != null) {
                _view.onUpdateMessage(text.toString());
            } else {
                _view.onUpdateMessage("Empty Response");
            }
        } else {
            _view.onUpdateMessage("Error: " + responseCode);
        }
    }
}