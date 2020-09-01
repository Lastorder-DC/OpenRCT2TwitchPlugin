(function () {
  "use strict";
  var port = 8000;
  var socket = network.createSocket();
  
  function update_twitch() {
    console.log("update_twitch");
    if(!socket.write('get_audiences\r\n')) console.log("get_audiences ERROR!");
  }

  function main() {
    socket.on('error', function(hadError) {
      if(hadError) {
        console.log("hadError true");
      } else {
        console.log("hadError false");
      }
    });
    
    socket.on('data', function(data) {
      if(data.indexOf("Connected") !== -1) {
        context.subscribe("interval.day", update_twitch);
      } else if(data == "ping") {
        socket.write('pong');
      } else {
        console.log("Received Data - " + data);
      }
    });
    
    socket.connect(port, "localhost", function() {
      console.log('socket connected');
      socket.write('CONNECT #lastorder_dc\r\n');
    });
  }

  registerPlugin({
    name: "openrct2-twitch-test",
    version: "0.0.1",
    licence: "BSD 2-Clause",
    authors: ["Lastorder-DC"],
    type: "remote",
    main: main,
  });
})();
